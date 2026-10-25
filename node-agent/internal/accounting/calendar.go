package accounting

import (
	"errors"
	"fmt"
	"time"

	// Embed the IANA database so a minimal node does not need /usr/share/zoneinfo.
	_ "time/tzdata"
)

var (
	// ErrCalendarZone rejects a missing, local or unknown accounting time zone.
	ErrCalendarZone = errors.New("accounting calendar: invalid time zone")
	// ErrCalendarBillingDay rejects a billing day outside 1 through 31.
	ErrCalendarBillingDay = errors.New("accounting calendar: billing day outside 1 through 31")
)

// Date is a civil calendar date in an accounting calendar.
type Date struct {
	Year  int
	Month time.Month
	Day   int
}

// String returns the date in YYYY-MM-DD form.
func (d Date) String() string {
	return fmt.Sprintf("%04d-%02d-%02d", d.Year, int(d.Month), d.Day)
}

// Calendar is one explicit accounting time zone and billing day.
//
// The zero value is unusable; callers must use NewCalendar.
//
// The caller freezes and supplies the calendar; it is never inferred from the
// node clock, provider metadata or a UTC fallback. A separately configured
// Calendar represents a provider reset schedule.
type Calendar struct {
	location   *time.Location
	billingDay int
}

// NewCalendar validates an explicit IANA zone and billing day.
func NewCalendar(zone string, billingDay int) (*Calendar, error) {
	if billingDay < 1 || billingDay > 31 {
		return nil, ErrCalendarBillingDay
	}
	if zone == "" || zone == "Local" {
		return nil, ErrCalendarZone
	}
	location, err := time.LoadLocation(zone)
	if err != nil || location == nil || location == time.Local {
		return nil, ErrCalendarZone
	}
	return &Calendar{location: location, billingDay: billingDay}, nil
}

// Cycle is the billing cycle containing one accounting local date.
type Cycle struct {
	LocalDate Date   // accounting local date of the resolved instant
	Key       string // YYYY-MM naming the month in which the cycle starts
	Start     Date   // inclusive cycle start
	End       Date   // inclusive cycle end
}

// Resolve returns the accounting local date and its cycle for an instant.
//
// Cycle dates use civil arithmetic in UTC. They never use a wall-clock
// duration or normalize through the accounting zone's local midnight, which
// can be skipped by a daylight-saving transition.
func (c *Calendar) Resolve(at time.Time) Cycle {
	local := at.In(c.location)
	localDate := Date{Year: local.Year(), Month: local.Month(), Day: local.Day()}

	year, month := localDate.Year, localDate.Month
	start := clampBillingDay(year, month, c.billingDay)
	if localDate.before(start) {
		previous := time.Date(year, month-1, 1, 0, 0, 0, 0, time.UTC)
		year, month = previous.Year(), previous.Month()
		start = clampBillingDay(year, month, c.billingDay)
	}

	following := time.Date(year, month+1, 1, 0, 0, 0, 0, time.UTC)
	nextStart := clampBillingDay(following.Year(), following.Month(), c.billingDay)
	end := time.Date(nextStart.Year, nextStart.Month, nextStart.Day-1, 0, 0, 0, 0, time.UTC)

	return Cycle{
		LocalDate: localDate,
		Key:       fmt.Sprintf("%04d-%02d", year, int(month)),
		Start:     start,
		End:       Date{Year: end.Year(), Month: end.Month(), Day: end.Day()},
	}
}

func clampBillingDay(year int, month time.Month, billingDay int) Date {
	// Day zero of the following month is the last day of this month.
	lastDay := time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
	if billingDay > lastDay {
		billingDay = lastDay
	}
	return Date{Year: year, Month: month, Day: billingDay}
}

func (d Date) before(other Date) bool {
	if d.Year != other.Year {
		return d.Year < other.Year
	}
	if d.Month != other.Month {
		return d.Month < other.Month
	}
	return d.Day < other.Day
}
