//! The part of a resolved runtime-access plan a container launch reads, and its labels.

use serde_json::Value;

use crate::Kind;

/// One plan root and whether any requirement writes it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Requirement {
    pub resource: String,
    pub path: String,
    pub write: bool,
}

/// The container plan of a brief's `runtime_access` (ADR 0025).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Plan {
    pub engine: Kind,
    pub worker_image: String,
    pub proxy_image: Option<String>,
    pub hosts: Vec<String>,
    /// Environment names only: the engine reads each value from its own environment.
    pub environment: Vec<String>,
    pub requirements: Vec<Requirement>,
    pub plan_digest: String,
}

fn text(value: &Value, key: &str) -> Result<String, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| format!("the plan has no {key}"))
}

fn texts(value: Option<&Value>, what: &str) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    value
        .as_array()
        .ok_or_else(|| format!("the plan {what} is not a list"))?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_owned)
                .ok_or_else(|| format!("the plan {what} holds a non-string"))
        })
        .collect()
}

impl Plan {
    /// The container plan in a resolved `runtime_access` plan.
    ///
    /// # Errors
    /// A plan without a container component, an unknown engine or a malformed field.
    pub fn parse(plan: &Value) -> Result<Self, String> {
        let container = plan
            .get("container")
            .filter(|value| value.is_object())
            .ok_or("the plan has no container component")?;
        let engine = text(container, "engine")?;
        let engine = Kind::parse(&engine).ok_or(format!("unknown container engine {engine}"))?;
        let requirements = plan
            .get("requirements")
            .and_then(Value::as_array)
            .ok_or("the plan has no requirements")?
            .iter()
            .map(|item| {
                let access = text(item, "access")?;
                if access != "read" && access != "write" {
                    return Err(format!("unknown plan access {access}"));
                }
                Ok(Requirement {
                    resource: text(item, "resource")?,
                    path: text(item, "path")?,
                    write: access == "write",
                })
            })
            .collect::<Result<Vec<_>, String>>()?;
        Ok(Self {
            engine,
            worker_image: text(container, "worker_image")?,
            proxy_image: container
                .get("proxy_image")
                .and_then(Value::as_str)
                .map(str::to_owned),
            hosts: texts(plan.get("network").and_then(|n| n.get("hosts")), "hosts")?,
            environment: texts(plan.get("environment"), "environment")?,
            requirements,
            plan_digest: text(plan, "plan_digest")?,
        })
    }

    /// The path of the first requirement of `resource`.
    pub fn root(&self, resource: &str) -> Option<&str> {
        self.requirements
            .iter()
            .find(|item| item.resource == resource)
            .map(|item| item.path.as_str())
    }

    /// Every distinct plan root, sorted.
    pub fn roots(&self) -> Vec<&str> {
        let mut roots: Vec<&str> = self
            .requirements
            .iter()
            .map(|item| item.path.as_str())
            .collect();
        roots.sort_unstable();
        roots.dedup();
        roots
    }
}

/// The host user a rootful engine runs the worker as, so files keep the host owner.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HostUser {
    pub uid: u32,
    pub gid: u32,
}

/// The labels of every object an execution creates (ADR 0022, section 2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Labels {
    pub dispatch: String,
    /// The runtime's own canonical Git common directory, never a request field.
    pub repository: String,
    pub plan_digest: String,
    pub operation: String,
    pub execution: String,
}

pub const LABEL_DISPATCH: &str = "harness.dispatch";
pub const LABEL_REPOSITORY: &str = "harness.repository";
pub const LABEL_PLAN_DIGEST: &str = "harness.plan_digest";
pub const LABEL_OPERATION: &str = "harness.operation";
pub const LABEL_EXECUTION: &str = "harness.execution";
