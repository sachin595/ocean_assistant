"""One place for the PDF filename - guest-facing title mapping.
"""

SOURCE_NAMES = {
    "01_knowledge_agent_kb.pdf": "Knowledge Agent Knowledge Base (Ship Policies)",
    "02_dining_agent_kb.pdf": "Dining Agent Knowledge Base",
    "03_entertainment_agent_kb.pdf": "Entertainment Agent Knowledge Base",
    "04_spa_wellness_kb.pdf": "Spa & Wellness Knowledge Base",
    "05_port_agent_kb.pdf": "Port Agent Knowledge Base",
    "06_excursion_agent_kb.pdf": "Excursion Agent Knowledge Base",
    "07_loyalty_agent_kb.pdf": "Loyalty Agent Knowledge Base",
    "08_billing_agent_kb.pdf": "Billing Agent Knowledge Base",
    "09_service_agent_kb.pdf": "Service Agent Knowledge Base",
    "10_booking_agent_kb.pdf": "Booking Agent Knowledge Base",
    "11_data_agent_kb.pdf": "Data Agent Knowledge Base",
    "12_escalation_feedback_kb.pdf": "Escalation & Feedback Knowledge Base",
}


def official_title(filename: str) -> str:
    """Title for a PDF filename; falls back to a cleaned-up filename so a
    new document never leaks a raw '.pdf' name to a guest."""
    if filename in SOURCE_NAMES:
        return SOURCE_NAMES[filename]
    return filename.replace(".pdf", "").replace("_", " ").title()
