-- One row per day of 2026, shared by every project that installs this package.
select toDate('2026-01-01') + number as calendar_date
from numbers(365)
