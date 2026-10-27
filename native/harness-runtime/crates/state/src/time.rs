//! The UTC time as the file ledger writes it: `datetime.now(UTC).isoformat()`, for example
//! `2026-10-22T07:10:19.972174+00:00`.

use std::time::{SystemTime, UNIX_EPOCH};

/// Now, as the ledger writes a time.
pub fn iso_now() -> String {
    iso_from_micros(now_micros())
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

/// The microseconds since the Unix epoch of an RFC 3339 time with a time zone, such as
/// `2026-10-22T07:10:19.972174+00:00` or `2026-10-22T07:10:19Z`; `None` when `text` is not one.
pub fn parse_rfc3339(text: &str) -> Option<i64> {
    let bytes = text.as_bytes();
    let number = |range: std::ops::Range<usize>| -> Option<i64> {
        let part = text.get(range)?;
        part.bytes()
            .all(|byte| byte.is_ascii_digit())
            .then(|| part.parse().ok())?
    };
    if bytes.len() < 20
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || !matches!(bytes[10], b'T' | b't' | b' ')
        || bytes[13] != b':'
        || bytes[16] != b':'
    {
        return None;
    }
    let (year, month, day) = (number(0..4)?, number(5..7)?, number(8..10)?);
    let (hour, minute, second) = (number(11..13)?, number(14..16)?, number(17..19)?);
    let mut at = 19;
    let mut micros = 0;
    if bytes[at] == b'.' {
        let start = at + 1;
        at = start;
        while at < bytes.len() && bytes[at].is_ascii_digit() {
            at += 1;
        }
        let digits = text.get(start..at)?;
        if digits.is_empty() {
            return None;
        }
        let kept: String = digits
            .chars()
            .chain(std::iter::repeat('0'))
            .take(6)
            .collect();
        micros = kept.parse::<i64>().ok()?;
    }
    let offset_seconds = match text.get(at..)? {
        "Z" | "z" => 0,
        zone if zone.len() == 6
            && matches!(zone.as_bytes()[0], b'+' | b'-')
            && zone.as_bytes()[3] == b':' =>
        {
            let hours = number(at + 1..at + 3)?;
            let minutes = number(at + 4..at + 6)?;
            if hours > 23 || minutes > 59 {
                return None;
            }
            let sign = if zone.as_bytes()[0] == b'-' { -1 } else { 1 };
            sign * (hours * 3600 + minutes * 60)
        }
        _ => return None,
    };
    let days_in_month = match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        2 => 28,
        _ => return None,
    };
    if day < 1 || day > days_in_month || hour > 23 || minute > 59 || second > 60 {
        return None;
    }
    let seconds = days_from_civil(year, month, day) * 86_400 + hour * 3600 + minute * 60 + second
        - offset_seconds;
    Some(seconds * 1_000_000 + micros)
}

/// The day count since 1970-01-01 of a proleptic Gregorian date (Howard Hinnant's algorithm).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let month_index = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_index + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Now, in microseconds since the Unix epoch.
pub fn now_micros() -> i64 {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    i64::try_from(elapsed.as_micros()).unwrap_or(i64::MAX)
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
    fn an_rfc_3339_time_is_read_back_to_its_microseconds() {
        for micros in [0, 951_782_400_000_000, 1_792_653_019_972_174] {
            assert_eq!(parse_rfc3339(&iso_from_micros(micros)), Some(micros));
        }
        assert_eq!(
            parse_rfc3339("2026-10-22T09:10:19.972174+02:00"),
            Some(1_792_653_019_972_174)
        );
        assert_eq!(parse_rfc3339("1970-01-01T00:00:00.5Z"), Some(500_000));
    }

    #[test]
    fn a_text_that_is_no_rfc_3339_time_is_refused() {
        for text in [
            "",
            "yesterday",
            "2026-10-22",
            "2026-10-22T07:10:19",
            "2026-02-30T00:00:00Z",
            "2026-13-01T00:00:00Z",
            "2026-10-22T25:00:00Z",
            "2026-10-22T07:10:19.Z",
            "2026-10-22T07:10:19+0000",
        ] {
            assert_eq!(parse_rfc3339(text), None, "{text}");
        }
    }

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
