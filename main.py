"""Ocean Cruises onboard assistant — terminal chat interface."""

import asyncio
import logging

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table as RichTable

from assistant.runtime import AssistantRuntime, UnknownGuestError
from assistant.session import AssistantSession
from assistant.telemetry import configure_logging
from config import PROJECT_ROOT, require_openai_key

configure_logging(PROJECT_ROOT / "assistant.log")

console = Console()

CONFIRM_WORDS = {"y", "yes", "confirm", "yes please", "yes, please", "confirmed"}
DECLINE_WORDS = {"n", "no", "cancel", "no thanks", "no, thanks"}


def sources_line(sources: list[str]) -> str:
    """One source reads inline; several get a bulleted list."""
    unique = list(dict.fromkeys(sources))
    if not unique:
        return ""
    if len(unique) == 1:
        return f"📄 Source: {unique[0]}"
    return "📄 Sources:\n" + "\n".join(f"  • {s}" for s in unique)


def show(turn) -> None:
    console.print(Panel(Markdown(turn.text), border_style="cyan",
                        title="Assistant", title_align="left"))
    if turn.data_table:
        dt = turn.data_table
        table = RichTable(title=dt.get("caption") or "From your onboard account",
                          title_style="dim", border_style="cyan")
        for col in dt["columns"]:
            table.add_column(str(col).replace("_", " ").title())
        for row in dt["rows"]:
            table.add_row(*["—" if v is None else str(v) for v in row])
        console.print(table)
    line = sources_line([c["source"] for c in (turn.citations or [])])
    if line:
        console.print(f"[dim]{line}[/dim]")


async def run() -> None:
    require_openai_key()
    console.print(Panel.fit(
        "🚢 [bold]Welcome aboard Ocean Cruises![/bold]\n"
        "I'm your onboard assistant — here for ship information, your\n"
        "onboard account, and your specialty dining reservations.",
        border_style="cyan"))

    runtime = AssistantRuntime()
    console.print("\n[dim]Starting services...[/dim]")
    await runtime.start()

    try:
        session: AssistantSession | None = None
        while session is None:
            guest_id = console.input(
                "\nPlease enter your [bold]Guest ID[/bold] "
                "(on your cruise card, e.g. G100005): ").strip()
            if not guest_id:
                continue
            try:
                session = await runtime.create_session(guest_id)
            except UnknownGuestError:
                console.print("[yellow]Hmm, I couldn't find that Guest ID. "
                              "Please check your cruise card and try again.[/yellow]")

        with console.status("[dim]connecting...[/dim]"):
            turn = await session.greet()
        show(turn)

        pending_id: str | None = None
        while True:
            try:
                user_input = console.input("[bold green]You:[/bold green] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[cyan]Enjoy the rest of your cruise! 🌊[/cyan]")
                break
            if not user_input:
                continue
            if user_input.lower() in {"quit", "exit", "bye"}:
                console.print("[cyan]Enjoy the rest of your cruise! 🌊[/cyan]")
                break

            try:
                lowered = user_input.lower()
                if pending_id and lowered in CONFIRM_WORDS:
                    with console.status("[dim]confirming...[/dim]"):
                        turn = await session.confirm_pending(pending_id)
                elif pending_id and lowered in DECLINE_WORDS:
                    with console.status("[dim]one moment...[/dim]"):
                        turn = await session.cancel_pending(pending_id)
                else:
                    with console.status("[dim]thinking...[/dim]"):
                        turn = await session.chat(user_input)

                show(turn)
                pending_id = (turn.pending_action or {}).get("action_id")
                if pending_id:
                    console.print("[dim]Reply 'yes' to confirm or 'no' to "
                                  "discard this action.[/dim]")
            except Exception:  # noqa: BLE001 — guest never sees a stack trace
                logging.getLogger("main").exception("turn_failed")
                console.print(Panel(
                    "I'm terribly sorry — something went wrong on my end. "
                    "Please try that again, or visit Guest Services on "
                    "Deck 5 for immediate help.",
                    border_style="red", title="Assistant", title_align="left"))
    finally:
        await runtime.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
