use std::{fmt, time::Duration};

use anyhow::Context;
use serde_json::{Value, json};
use tokio::{
    net::TcpStream,
    time::{Instant, timeout},
};
use tokio_rustls::TlsConnector;

use crate::{
    addon,
    connection::{self, BlenderConnection},
    security,
};

struct Check {
    name: &'static str,
    status: &'static str,
    detail: String,
    data: Value,
}

#[derive(Default)]
pub struct Report {
    checks: Vec<Check>,
}

impl Report {
    fn add(
        &mut self,
        name: &'static str,
        status: &'static str,
        detail: impl Into<String>,
        data: Value,
    ) {
        self.checks.push(Check {
            name,
            status,
            detail: detail.into(),
            data,
        });
    }

    fn unavailable(&mut self, names: &[&'static str], reason: &str) {
        for name in names {
            self.add(name, "unavailable", reason, Value::Null);
        }
    }

    pub fn healthy(&self) -> bool {
        self.checks.iter().all(|check| check.status != "error")
    }

    pub fn json(&self) -> Value {
        json!({
            "healthy": self.healthy(),
            "checks": self.checks.iter().map(|check| json!({
                "name": check.name, "status": check.status,
                "detail": check.detail, "data": check.data,
            })).collect::<Vec<_>>()
        })
    }
}

impl fmt::Display for Report {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        writeln!(f, "blender-mcp doctor")?;
        for check in &self.checks {
            let detail: String = check
                .detail
                .chars()
                .flat_map(|character| {
                    if character.is_control() {
                        character.escape_default().collect::<Vec<_>>()
                    } else {
                        vec![character]
                    }
                })
                .collect();
            writeln!(f, "[{}] {}: {}", check.status, check.name, detail)?;
        }
        writeln!(
            f,
            "\nStatus: {}",
            if !self.healthy() {
                "not ready"
            } else if self.checks.iter().any(|check| check.status == "warning") {
                "ready with warnings"
            } else {
                "ready"
            }
        )
    }
}

const ADDON_CHECKS: &[&str] = &[
    "addon",
    "addon_version",
    "protocol",
    "capabilities",
    "blender_version",
    "blender_binary",
    "background",
    "online_access",
    "file",
    "scene",
    "mode",
    "listener",
    "addon_credentials",
    "sketchfab",
];

pub async fn check(deadline: Duration) -> Report {
    let mut report = Report::default();
    report.add(
        "server",
        "ok",
        format!(
            "{} ({}/{}); protocol {}; Blender >= {}",
            env!("CARGO_PKG_VERSION"),
            std::env::consts::OS,
            std::env::consts::ARCH,
            addon::PROTOCOL_VERSION,
            addon::BLENDER_VERSION_MIN
        ),
        json!({"version": env!("CARGO_PKG_VERSION"), "protocol": addon::PROTOCOL_VERSION,
            "blender_version_min": addon::BLENDER_VERSION_MIN, "os": std::env::consts::OS,
            "arch": std::env::consts::ARCH}),
    );
    match std::env::current_exe() {
        Ok(path) => report.add(
            "executable",
            "info",
            path.display().to_string(),
            json!(path),
        ),
        Err(error) => report.add("executable", "warning", error.to_string(), Value::Null),
    }

    let host = std::env::var("BLENDER_HOST").unwrap_or_else(|_| "localhost".into());
    let port = std::env::var("BLENDER_PORT").unwrap_or_else(|_| "9876".into());
    let endpoint = connection::endpoint();
    let endpoint_data = json!({"host": host, "port": port, "timeout_seconds": deadline.as_secs_f64(),
        "host_source": if std::env::var_os("BLENDER_HOST").is_some() { "BLENDER_HOST" } else { "default" },
        "port_source": if std::env::var_os("BLENDER_PORT").is_some() { "BLENDER_PORT" } else { "default" }});
    match &endpoint {
        Ok(_) => report.add(
            "endpoint",
            "ok",
            format!(
                "{host}:{port}; timeout {}s per check",
                deadline.as_secs_f64()
            ),
            endpoint_data,
        ),
        Err(error) => report.add("endpoint", "error", error.to_string(), endpoint_data),
    }

    let credentials = security::directory().and_then(|directory| {
        report.add(
            "credential_directory",
            "info",
            directory.display().to_string(),
            json!(directory),
        );
        let data =
            security::load(&directory).context("Credential storage or contents are invalid")?;
        security::validate_setup(&data)?;
        security::client_config(&data)
    });
    match &credentials {
        Ok(_) => report.add(
            "credentials",
            "ok",
            "Credential storage and TLS key pairs valid",
            Value::Null,
        ),
        Err(error) => report.add("credentials", "error", format!("{error:#}"), Value::Null),
    }

    let Ok((host, port)) = endpoint else {
        report.unavailable(&["tcp", "tls"], "Invalid endpoint configuration");
        report.unavailable(ADDON_CHECKS, "No authenticated connection");
        return report;
    };
    let started = Instant::now();
    let tcp = match timeout(deadline, TcpStream::connect((host.as_str(), port))).await {
        Ok(Ok(tcp)) => {
            let peer = tcp
                .peer_addr()
                .map(|address| address.to_string())
                .unwrap_or_default();
            report.add(
                "tcp",
                "ok",
                format!("Connected to {peer} ({} ms)", started.elapsed().as_millis()),
                json!({"peer": peer, "elapsed_ms": started.elapsed().as_millis()}),
            );
            tcp
        }
        result => {
            let detail = match result {
                Ok(Err(error)) => error.to_string(),
                _ => format!("Connection timed out after {}s", deadline.as_secs_f64()),
            };
            report.add("tcp", "error", detail, Value::Null);
            report.unavailable(&["tls"], "TCP connection unavailable");
            report.unavailable(ADDON_CHECKS, "No authenticated connection");
            return report;
        }
    };
    let Ok(config) = credentials else {
        report.unavailable(&["tls"], "Valid local credentials unavailable");
        report.unavailable(ADDON_CHECKS, "No authenticated connection");
        return report;
    };
    let tls = match timeout(
        deadline,
        TlsConnector::from(config).connect(
            security::SERVER_NAME
                .try_into()
                .expect("constant TLS server name"),
            tcp,
        ),
    )
    .await
    {
        Ok(Ok(tls)) => {
            let session = tls.get_ref().1;
            let protocol = format!("{:?}", session.protocol_version().unwrap());
            let cipher = format!("{:?}", session.negotiated_cipher_suite().unwrap().suite());
            report.add("tls", "ok", format!("{protocol}; {cipher}; paired server authenticated"),
                json!({"protocol": protocol, "cipher": cipher, "server_name": security::SERVER_NAME}));
            tls
        }
        result => {
            let detail = match result {
                Ok(Err(error)) => error.to_string(),
                _ => format!("TLS handshake timed out after {}s", deadline.as_secs_f64()),
            };
            report.add("tls", "error", detail, Value::Null);
            report.unavailable(ADDON_CHECKS, "No authenticated connection");
            return report;
        }
    };
    let connection = BlenderConnection::connected(host, port, tls);
    let started = Instant::now();
    let info = timeout(deadline, connection.send("get_addon_info", json!({}))).await;
    match info {
        Ok(Ok(info)) if info.is_object() => {
            report.add(
                "addon",
                "ok",
                format!(
                    "Responding ({} ms); mutual TLS accepted",
                    started.elapsed().as_millis()
                ),
                Value::Null,
            );
            addon_status(&mut report, &info);
        }
        result => {
            let detail = match result {
                Ok(Err(error)) => format!("{error:#}"),
                Ok(Ok(_)) => "Invalid add-on status: expected an object".into(),
                Err(_) => format!(
                    "No status response after {}s; Blender may be busy",
                    deadline.as_secs_f64()
                ),
            };
            report.add("addon", "error", detail, Value::Null);
            report.unavailable(&ADDON_CHECKS[1..], "Add-on status unavailable");
        }
    }
    report
}

fn version(value: &str) -> Option<[u64; 3]> {
    let mut parts = value.split('.').map(|part| {
        part.chars()
            .take_while(char::is_ascii_digit)
            .collect::<String>()
            .parse()
            .ok()
    });
    Some([parts.next()??, parts.next()??, parts.next()??])
}

fn addon_status(report: &mut Report, info: &Value) {
    let release = info["addon_build_version"].as_str();
    report.add(
        "addon_version",
        if release == Some(env!("CARGO_PKG_VERSION")) {
            "ok"
        } else {
            "warning"
        },
        format!(
            "{}; bundled {}",
            release.unwrap_or("not reported"),
            env!("CARGO_PKG_VERSION")
        ),
        json!({"reported": release, "expected": env!("CARGO_PKG_VERSION")}),
    );
    let protocol = info["protocol_version"].as_u64();
    report.add(
        "protocol",
        if protocol == Some(addon::PROTOCOL_VERSION) {
            "ok"
        } else {
            "error"
        },
        format!(
            "{}; expected {}",
            protocol
                .map(|value| value.to_string())
                .unwrap_or_else(|| "not reported".into()),
            addon::PROTOCOL_VERSION
        ),
        json!({"reported": protocol, "expected": addon::PROTOCOL_VERSION}),
    );
    match info["capabilities"]
        .as_array()
        .and_then(|values| values.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
    {
        Some(capabilities) => report.add(
            "capabilities",
            "info",
            format!("{}: {}", capabilities.len(), capabilities.join(", ")),
            info["capabilities"].clone(),
        ),
        None => report.unavailable(&["capabilities"], "Not reported by this add-on"),
    }
    let blender = info["blender_version"].as_str().unwrap_or("not reported");
    let status = match version(blender) {
        Some(value) if Some(value) >= version(addon::BLENDER_VERSION_MIN) => "ok",
        Some(_) => "error",
        None => "warning",
    };
    report.add(
        "blender_version",
        status,
        format!("{blender}; minimum {}", addon::BLENDER_VERSION_MIN),
        json!({"reported": info["blender_version"], "minimum": addon::BLENDER_VERSION_MIN}),
    );

    let runtime = &info["runtime"];
    for (name, key) in [
        ("blender_binary", "blender_binary"),
        ("background", "background"),
        ("online_access", "online_access"),
        ("file", "file"),
        ("scene", "scene"),
        ("mode", "mode"),
        ("listener", "listener"),
        ("addon_credentials", "credential_directory"),
    ] {
        match runtime.get(key) {
            Some(value) => report.add(name, "info", runtime_detail(name, value), value.clone()),
            None => report.unavailable(&[name], "Not reported by this add-on"),
        }
    }
    let sketchfab = &runtime["sketchfab"];
    match (
        sketchfab["enabled"].as_bool(),
        sketchfab["api_key_configured"].as_bool(),
    ) {
        (Some(enabled), Some(key)) => report.add(
            "sketchfab",
            if enabled && !key { "warning" } else { "info" },
            format!(
                "{}; API key {}; external API not checked",
                if enabled { "enabled" } else { "disabled" },
                if key { "configured" } else { "not configured" }
            ),
            json!({"enabled": enabled, "api_key_configured": key, "api_checked": false}),
        ),
        _ => report.unavailable(&["sketchfab"], "Not reported by this add-on"),
    }
}

fn runtime_detail(name: &str, value: &Value) -> String {
    match name {
        "background" | "online_access" if value.is_boolean() => {
            if value == true { "enabled" } else { "disabled" }.into()
        }
        "file" if value["path"].is_string() && value["dirty"].is_boolean() => {
            let path = value["path"].as_str().unwrap();
            format!(
                "{}; {}",
                if path.is_empty() {
                    "unsaved file"
                } else {
                    path
                },
                if value["dirty"] == true {
                    "unsaved changes"
                } else {
                    "no unsaved changes"
                }
            )
        }
        "listener"
            if value["running"].is_boolean()
                && value["host"].is_string()
                && value["port"].is_u64() =>
        {
            format!(
                "{} at {}:{}",
                if value["running"] == true {
                    "running"
                } else {
                    "stopped"
                },
                value["host"].as_str().unwrap(),
                value["port"]
            )
        }
        "scene" if value.is_null() => "none".into(),
        _ => value
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| value.to_string()),
    }
}
