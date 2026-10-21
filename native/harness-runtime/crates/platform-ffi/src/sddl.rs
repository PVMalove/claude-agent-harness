//! A reader for the DACL part of an SDDL string. The Windows tests compare parsed ACEs instead of
//! the exact text, because Windows may abbreviate or reorder what it prints.

/// One access control entry: `(type;flags;rights;object;inherit_object;trustee)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Ace {
    /// `A` allow, `D` deny, and so on.
    pub kind: String,
    pub flags: String,
    pub rights: String,
    /// A SID string or a two-letter alias.
    pub trustee: String,
}

/// A discretionary ACL.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Dacl {
    /// `P`: the ACL does not inherit entries from its parent.
    pub protected: bool,
    pub aces: Vec<Ace>,
}

/// Parse `D:<flags>(<ace>)(<ace>)...`.
pub fn parse_dacl(sddl: &str) -> Result<Dacl, String> {
    let body = sddl
        .strip_prefix("D:")
        .ok_or_else(|| format!("not a DACL: {sddl}"))?;
    let (flags, mut rest) = body.split_at(body.find('(').unwrap_or(body.len()));
    let mut aces = Vec::new();
    while !rest.is_empty() {
        let inner = rest
            .strip_prefix('(')
            .and_then(|tail| tail.split_once(')'))
            .ok_or_else(|| format!("malformed ACE list: {rest}"))?;
        let fields: Vec<&str> = inner.0.split(';').collect();
        let [kind, ace_flags, rights, _object, _inherit, trustee, ..] = fields[..] else {
            return Err(format!("an ACE needs six fields: {}", inner.0));
        };
        aces.push(Ace {
            kind: kind.to_owned(),
            flags: ace_flags.to_owned(),
            rights: rights.to_owned(),
            trustee: trustee.to_owned(),
        });
        rest = inner.1;
    }
    Ok(Dacl {
        protected: flags.contains('P'),
        aces,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_protected_dacl_with_one_allow_entry_is_read_field_by_field() {
        let dacl = parse_dacl("D:P(A;;FA;;;S-1-5-21-1-2-3-1001)").unwrap();

        assert!(dacl.protected);
        assert_eq!(
            dacl.aces,
            [Ace {
                kind: "A".into(),
                flags: String::new(),
                rights: "FA".into(),
                trustee: "S-1-5-21-1-2-3-1001".into(),
            }]
        );
    }

    #[test]
    fn inherited_entries_and_several_aces_keep_their_order() {
        let dacl = parse_dacl("D:AI(A;;FA;;;SY)(D;OICI;FR;;;WD)").unwrap();

        assert!(!dacl.protected);
        assert_eq!(dacl.aces.len(), 2);
        assert_eq!(dacl.aces[0].trustee, "SY");
        assert_eq!(
            (dacl.aces[1].kind.as_str(), dacl.aces[1].flags.as_str()),
            ("D", "OICI")
        );
    }

    #[test]
    fn an_empty_dacl_has_no_entries() {
        assert_eq!(parse_dacl("D:P").unwrap().aces, []);
    }

    #[test]
    fn text_that_is_not_a_dacl_is_refused() {
        assert!(parse_dacl("O:BAG:BA").is_err());
        assert!(parse_dacl("D:P(A;;FA;;;SY").is_err());
        assert!(parse_dacl("D:P(A;;FA)").is_err());
    }
}
