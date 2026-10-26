//! Records to rows and back.
//!
//! A table of `Spec` carries some fields of its legacy record as typed columns; the rest of the
//! record is `body_json`. `split` and `join` are inverse for every record: a typed field leaves
//! the body only when its value has the column's type.

use rusqlite::types::Value as Sql;
use serde_json::{Map, Value};

/// The type of a typed column.
#[derive(Clone, Copy, Debug)]
pub enum Column {
    Text,
    /// A JSON boolean, stored as 0 or 1.
    Bool,
}

/// One table of a legacy record directory.
#[derive(Debug)]
pub struct Spec {
    pub table: &'static str,
    /// The record directory of the generation.
    pub directory: &'static str,
    /// The typed columns; each is named like the field it carries.
    pub typed: &'static [(&'static str, Column)],
}

pub const BATCH: Spec = Spec {
    table: "batch",
    directory: "batches",
    typed: &[
        ("state", Column::Text),
        ("risk_reassessment_required", Column::Bool),
        ("risk_reassessment_candidate", Column::Text),
        ("risk_reassessment_pending_next", Column::Bool),
    ],
};
pub const PLAN: Spec = Spec {
    table: "plan",
    directory: "plans",
    typed: &[],
};
pub const DISPATCH: Spec = Spec {
    table: "dispatch",
    directory: "dispatches",
    typed: &[("batch_id", Column::Text), ("state", Column::Text)],
};
pub const DISPATCH_STATUS: Spec = Spec {
    table: "dispatch_status",
    directory: "dispatch-status",
    typed: &[("state", Column::Text)],
};
pub const RISK_ASSESSMENT: Spec = Spec {
    table: "risk_assessment",
    directory: "risk-assessments",
    typed: &[],
};
pub const CONTEXT_PACKAGE: Spec = Spec {
    table: "context_package",
    directory: "context-packages",
    typed: &[],
};
pub const CHECKPOINT: Spec = Spec {
    table: "checkpoint",
    directory: "checkpoints",
    typed: &[],
};

/// Every table that holds the records of one directory, in foreign-key order.
pub const SPECS: [&Spec; 7] = [
    &BATCH,
    &PLAN,
    &DISPATCH,
    &DISPATCH_STATUS,
    &RISK_ASSESSMENT,
    &CONTEXT_PACKAGE,
    &CHECKPOINT,
];

/// The spec of a record directory.
pub fn spec_of(directory: &str) -> Option<&'static Spec> {
    SPECS.into_iter().find(|spec| spec.directory == directory)
}

impl Spec {
    /// `INSERT` of one row: `id`, the typed columns, `body_json`.
    pub fn insert_sql(&self) -> String {
        let columns: Vec<&str> = self.typed.iter().map(|(name, _)| *name).collect();
        let marks = vec!["?"; columns.len() + 2].join(", ");
        let names = if columns.is_empty() {
            String::new()
        } else {
            format!("{}, ", columns.join(", "))
        };
        format!(
            "INSERT INTO {} (id, {names}body_json) VALUES ({marks})",
            self.table
        )
    }

    /// Whether the rows of the table carry a `revision` that a write checks and advances.
    pub fn revisioned(&self) -> bool {
        matches!(self.table, "batch" | "dispatch" | "dispatch_status")
    }

    /// Whether a command may replace a row of the table. The other tables hold immutable evidence.
    pub fn replaceable(&self) -> bool {
        matches!(self.table, "batch" | "dispatch_status")
    }

    /// `SELECT` of every row, ordered by `id`: the same columns, in the same order, and, for a
    /// revisioned table, the `revision` after `body_json`.
    pub fn select_sql(&self) -> String {
        let columns: Vec<&str> = self.typed.iter().map(|(name, _)| *name).collect();
        let names = if columns.is_empty() {
            String::new()
        } else {
            format!("{}, ", columns.join(", "))
        };
        let revision = if self.revisioned() { ", revision" } else { "" };
        format!(
            "SELECT id, {names}body_json{revision} FROM {} ORDER BY id",
            self.table
        )
    }

    /// `UPDATE` of one row by `id`: the typed columns and `body_json` are set, and the revision
    /// advances by one. The parameters are those of `split` with `id` moved to the end.
    pub fn update_sql(&self) -> String {
        let mut assignments: Vec<String> = self
            .typed
            .iter()
            .map(|(name, _)| format!("{name} = ?"))
            .collect();
        assignments.push("body_json = ?".to_owned());
        assignments.push("revision = revision + 1".to_owned());
        format!(
            "UPDATE {} SET {} WHERE id = ?",
            self.table,
            assignments.join(", ")
        )
    }

    /// The parameters of the insert for one record, `id` first and `body_json` last.
    pub fn split(&self, id: &str, mut record: Map<String, Value>) -> Vec<Sql> {
        let mut params = vec![Sql::Text(id.to_owned())];
        for (name, column) in self.typed {
            let typed = match (column, record.get(*name)) {
                (Column::Text, Some(Value::String(text))) => Some(Sql::Text(text.clone())),
                (Column::Bool, Some(Value::Bool(flag))) => Some(Sql::Integer(i64::from(*flag))),
                _ => None,
            };
            match typed {
                Some(value) => {
                    record.remove(*name);
                    params.push(value);
                }
                None => params.push(Sql::Null),
            }
        }
        params.push(Sql::Text(Value::Object(record).to_string()));
        params
    }

    /// The legacy record of one row read in the order of `select_sql`.
    pub fn join(&self, row: &rusqlite::Row<'_>) -> rusqlite::Result<(String, Map<String, Value>)> {
        let id: String = row.get(0)?;
        let body: String = row.get(self.typed.len() + 1)?;
        let Ok(Value::Object(mut record)) = serde_json::from_str::<Value>(&body) else {
            return Err(rusqlite::Error::InvalidColumnType(
                self.typed.len() + 1,
                "body_json".to_owned(),
                rusqlite::types::Type::Text,
            ));
        };
        for (index, (name, column)) in self.typed.iter().enumerate() {
            let value: Sql = row.get(index + 1)?;
            match (column, value) {
                (Column::Text, Sql::Text(text)) => {
                    record.insert((*name).to_owned(), Value::String(text));
                }
                (Column::Bool, Sql::Integer(flag)) => {
                    record.insert((*name).to_owned(), Value::Bool(flag != 0));
                }
                _ => {}
            }
        }
        Ok((id, record))
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn round_trip(spec: &Spec, record: Value) -> Value {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection
            .execute_batch("PRAGMA foreign_keys = OFF;")
            .unwrap();
        connection.execute_batch(crate::schema::SCHEMA).unwrap();
        let Value::Object(members) = record else {
            panic!("a record is an object")
        };
        let params = spec.split("x", members);
        connection
            .execute(&spec.insert_sql(), rusqlite::params_from_iter(params))
            .unwrap();
        let (_, back) = connection
            .query_row(&spec.select_sql(), [], |row| spec.join(row))
            .unwrap();
        Value::Object(back)
    }

    #[test]
    fn a_typed_field_leaves_the_body_and_comes_back() {
        let record = json!({
            "state": "active",
            "risk_reassessment_required": true,
            "risk_reassessment_candidate": "abc",
            "risk_reassessment_pending_next": false,
            "other": [1, 2.50, 1e-5],
        });

        assert_eq!(round_trip(&BATCH, record.clone()), record);
    }

    #[test]
    fn a_field_of_another_type_stays_in_the_body_and_the_column_is_null() {
        let record = json!({
            "state": "active",
            "risk_reassessment_required": "yes",
            "risk_reassessment_candidate": null,
            "risk_reassessment_pending_next": 1,
        });
        let Value::Object(members) = record.clone() else {
            panic!("an object")
        };

        let params = BATCH.split("b", members);

        assert_eq!(params[2], Sql::Null);
        assert_eq!(params[3], Sql::Null);
        assert_eq!(params[4], Sql::Null);
        assert_eq!(round_trip(&BATCH, record.clone()), record);
    }

    #[test]
    fn an_absent_typed_field_stays_absent() {
        let record = json!({"state": "active"});

        assert_eq!(round_trip(&BATCH, record.clone()), record);
    }
}
