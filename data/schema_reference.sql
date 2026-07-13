-- Data Schema Reference
-- All data is provided as CSV files in the data/ directory.

-- GUEST PROFILES (data/guest_profiles.csv) — 200 guests
-- Columns:
--   Guest_ID, First_Name, Last_Name, Cabin_Number, Cabin_Category,
--   Deck, Loyalty_Tier, Loyalty_Points, Party_Size, Embark_Date,
--   Debark_Date, Dietary_Restrictions, Special_Occasions, Beverage_Package, Past_Cruises

-- FOLIO TRANSACTIONS (data/folio_transactions.csv) — 4,775 transactions
-- Columns:
--   Transaction_ID, Guest_ID, Cabin_Number, Transaction_Date, Transaction_Time,
--   Category, Description, Venue, Quantity, Unit_Price, Amount,
--   Service_Charge, Total, Status, Reference_ID, Posted_By, Notes

-- DINING RESERVATIONS (data/dining_reservations.csv) — 212 reservations
-- Columns:
--   Reservation_ID, Guest_ID, Guest_Name, Cabin_Number, Venue_ID, Venue_Name,
--   Reservation_Date, Reservation_Time, Party_Size, Special_Requests,
--   Dietary_Notes, Status, Confirmation_Number, Created_At, Modified_At,
--   Cancelled_At, Cancellation_Reason

-- RELATIONSHIPS:
--   folio_transactions.Guest_ID → guest_profiles.Guest_ID
--   dining_reservations.Guest_ID → guest_profiles.Guest_ID
