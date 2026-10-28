//! What `<engine> info` and `<engine> image inspect` say: the engine mode, SELinux, the engine
//! identity and image ids. A one-to-one port of `_docker_facts`, `_podman_facts` and `_image_id`.

use serde_json::Value;

/// How the engine runs containers relative to the host user.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Rootful,
    Rootless,
    /// Docker with user namespace remapping: not supported.
    UsernsRemap,
    /// Podman whose service is remote: not supported.
    Remote,
    /// `info` did not answer.
    Unavailable,
    /// `info` answered something that is not this engine.
    Unknown,
}

impl Mode {
    /// The machine value of the evidence.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Rootful => "rootful",
            Self::Rootless => "rootless",
            Self::UsernsRemap => "userns-remap",
            Self::Remote => "remote",
            Self::Unavailable => "unavailable",
            Self::Unknown => "unknown",
        }
    }
}

/// The facts of one `info` answer.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Facts {
    pub mode: Mode,
    pub selinux: bool,
    /// `docker:<daemon id>` or `podman:<hostname>`: a changed value means another engine.
    pub environment_id: String,
}

fn nonempty_text(value: Option<&Value>) -> Option<&str> {
    value
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

/// The facts of a Docker `info` answer; `None` if it is not one.
pub fn docker_facts(info: &Value) -> Option<Facts> {
    let options = info.get("SecurityOptions")?.as_array()?;
    nonempty_text(info.get("ServerVersion"))?;
    let id = nonempty_text(info.get("ID"))?;
    let names: Vec<&str> = options
        .iter()
        .filter_map(Value::as_str)
        .map(|option| option.split(',').next().unwrap_or_default())
        .collect();
    let mode = if names.contains(&"name=rootless") {
        Mode::Rootless
    } else if names.contains(&"name=userns") {
        Mode::UsernsRemap
    } else {
        Mode::Rootful
    };
    Some(Facts {
        mode,
        selinux: names.contains(&"name=selinux"),
        environment_id: format!("docker:{id}"),
    })
}

/// The facts of a Podman `info` answer; `None` if it is not one.
pub fn podman_facts(info: &Value) -> Option<Facts> {
    let host = info.get("host")?.as_object()?;
    let hostname = nonempty_text(host.get("hostname"))?;
    let security = host.get("security")?.as_object()?;
    let rootless = security.get("rootless")?.as_bool()?;
    let selinux = security.get("selinuxEnabled")?.as_bool()?;
    nonempty_text(info.get("version")?.as_object()?.get("Version"))?;
    let mode = if host.get("serviceIsRemote") == Some(&Value::Bool(true)) {
        Mode::Remote
    } else if rootless {
        Mode::Rootless
    } else {
        Mode::Rootful
    };
    Some(Facts {
        mode,
        selinux,
        environment_id: format!("podman:{hostname}"),
    })
}

fn is_hex64(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// Whether `text` is an image id the brief may pin: `sha256:` and 64 lowercase hex digits.
pub fn is_image_id(text: &str) -> bool {
    text.strip_prefix("sha256:").is_some_and(is_hex64)
}

/// Whether `text` is a full container or network id.
pub fn is_object_id(text: &str) -> bool {
    is_hex64(text)
}

/// The image id in an `image inspect --format {{.Id}}` answer; Podman prints it bare.
pub fn image_id(output: &str) -> Option<String> {
    if is_hex64(output) {
        return Some(format!("sha256:{output}"));
    }
    is_image_id(output).then(|| output.to_owned())
}
