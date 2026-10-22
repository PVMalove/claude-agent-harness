//! The UTC time as the file ledger writes it: `datetime.now(UTC).isoformat()`, for example
//! `2026-10-22T07:10:19.972174+00:00`.

use std::time::{SystemTime, UNIX_EPOCH};

/// Now, as the ledger writes a time.
pub fn iso_now() -> String {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    iso_from_micros(i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX))
}

/// The ledger's text for a time given in microseconds since the Unix epoch.
pub fn iso_from_micros(micros: i64) -> String {
    let seconds = micros.div_euclid(1_000_000);
    let fraction = micros.rem_euclid(1_000_000);
    let days = seconds.div_euclid(86_400);
    let day_seconds = seconds.rem_euclid(86_400);
    let (year, month, day) = civil_from_days(days);
    let (hour, minute, second) = (
        day_seconds / 3600,
        day_seconds % 3600 / 60,
        day_seconds % 60,
    );
    // Python leaves the fraction out when it is zero.
    let fraction = if fraction == 0 {
        String::new()
    } else {
        format!(".{fraction:06}")
    };
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}{fraction}+00:00")
}

/// The proleptic Gregorian date of a day count since 1970-01-01 (Howard Hinnant's algorithm).
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_time_is_written_as_python_writes_it() {
        // 2026-10-22T07:10:19.972174+00:00 as Python prints it.
        assert_eq!(
            iso_from_micros(1_792_653_019_972_174),
            "2026-10-22T07:10:19.972174+00:00"
        );
        assert_eq!(iso_from_micros(0), "1970-01-01T00:00:00+00:00");
        assert_eq!(
            iso_from_micros(951_782_400_000_000),
            "2000-02-29T00:00:00+00:00"
        );
    }
}
