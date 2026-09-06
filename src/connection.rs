use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
    sync::Mutex,
    time::timeout,
};

pub const MAX_MESSAGE_BYTES: usize = 32 * 1024 * 1024;

pub struct BlenderConnection {
    host: String,
    port: u16,
    stream: Mutex<Option<TcpStream>>,
    deadline: Duration,
}

impl BlenderConnection {
    pub fn new(host: String, port: u16) -> Self {
        Self {
            host,
            port,
            stream: Mutex::new(None),
            deadline: Duration::from_secs(180),
        }
    }

    pub async fn send(&self, command: &str, params: Value) -> Result<Value> {
        let mut slot = self.stream.lock().await;
        // Own the socket across awaits so cancellation closes a partially used stream.
        let mut stream = match slot.take() {
            Some(stream) => stream,
            None => timeout(
                Duration::from_secs(5),
                TcpStream::connect((self.host.as_str(), self.port)),
            )
            .await
            .context("Timed out connecting to Blender")?
            .context("Could not connect to Blender; start the MCP add-on in Blender")?,
        };
        let response = timeout(self.deadline, exchange(&mut stream, command, params)).await
            .context("Timed out waiting for Blender; the command may still run. Check the scene before retrying")??;
        *slot = Some(stream);
        match response["status"].as_str() {
            Some("success") => {
                let result = response
                    .get("result")
                    .context("Blender response has no result")?;
                if let Some(error) = result.get("error").filter(|e| !e.is_null()) {
                    bail!("Blender: {error}");
                }
                if result.get("success") == Some(&Value::Bool(false)) {
                    bail!("Blender: {}", result.get("message").unwrap_or(result));
                }
                Ok(result.clone())
            }
            Some("error") => bail!("Blender: {}", response["message"]),
            _ => {
                *slot = None;
                bail!("Invalid Blender response status");
            }
        }
    }
}

async fn exchange(stream: &mut TcpStream, command: &str, params: Value) -> Result<Value> {
    let request = serde_json::to_vec(&json!({"type": command, "params": params}))?;
    if request.len() > MAX_MESSAGE_BYTES {
        bail!("Blender command exceeds the 32 MiB limit");
    }
    stream
        .write_all(&request)
        .await
        .context("Connection lost sending command; check Blender before retrying")?;
    let mut response = Vec::new();
    let mut chunk = [0u8; 8192];
    // Scan each byte once before parsing the complete JSON response.
    let mut frame = JsonFrame::default();
    loop {
        let read = stream
            .read(&mut chunk)
            .await
            .context("Connection lost receiving Blender response")?;
        if read == 0 {
            bail!(
                "Blender closed the connection before a complete response; check the scene before retrying"
            );
        }
        if response.len() + read > MAX_MESSAGE_BYTES {
            bail!("Blender response exceeds the 32 MiB limit");
        }
        response.extend_from_slice(&chunk[..read]);
        if frame.consume(&chunk[..read])? {
            return serde_json::from_slice(&response).context("Invalid JSON from Blender");
        }
    }
}

#[derive(Default)]
struct JsonFrame {
    depth: usize,
    started: bool,
    quoted: bool,
    escaped: bool,
}

impl JsonFrame {
    fn consume(&mut self, bytes: &[u8]) -> Result<bool> {
        for &byte in bytes {
            if !self.started {
                if byte.is_ascii_whitespace() {
                    continue;
                }
                if byte != b'{' {
                    bail!("Invalid JSON from Blender: expected an object");
                }
                self.started = true;
            }
            if self.quoted {
                if self.escaped {
                    self.escaped = false;
                } else if byte == b'\\' {
                    self.escaped = true;
                } else if byte == b'"' {
                    self.quoted = false;
                }
            } else {
                match byte {
                    b'"' => self.quoted = true,
                    b'{' | b'[' => {
                        self.depth += 1;
                        if self.depth > 128 {
                            bail!("Invalid JSON from Blender: nesting exceeds 128 levels");
                        }
                    }
                    b'}' | b']' => {
                        self.depth -= 1;
                        if self.depth == 0 {
                            return Ok(true);
                        }
                    }
                    _ => {}
                }
            }
        }
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    #[test]
    fn framing_ignores_escaped_quotes_and_brackets_inside_strings() {
        let response = serde_json::to_vec(
            &json!({"status":"success","result":["quote\" slash\\ brackets}{[] 立方体", {}]}),
        )
        .unwrap();
        for split in 0..response.len() {
            let mut frame = JsonFrame::default();
            assert!(!frame.consume(&response[..split]).unwrap());
            assert!(frame.consume(&response[split..]).unwrap());
        }
    }

    async fn read_request(stream: &mut TcpStream) -> Value {
        let mut data = Vec::new();
        loop {
            data.push(stream.read_u8().await.unwrap());
            if let Ok(value) = serde_json::from_slice(&data) {
                return value;
            }
        }
    }

    #[tokio::test]
    async fn serializes_commands_and_handles_fragmented_unicode() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let connection =
            BlenderConnection::new("127.0.0.1".into(), listener.local_addr().unwrap().port());
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            for _ in 0..2 {
                let request = read_request(&mut stream).await;
                let response = serde_json::to_vec(&json!({"status":"success", "result":{"name":"立方体 🧊", "command":request["type"]}})).unwrap();
                for byte in response {
                    stream.write_all(&[byte]).await.unwrap();
                    tokio::task::yield_now().await;
                }
            }
        });
        let (first, second) = tokio::join!(
            connection.send("first", json!({})),
            connection.send("second", json!({}))
        );
        assert_eq!(
            first.unwrap(),
            json!({"name":"立方体 🧊","command":"first"})
        );
        assert_eq!(second.unwrap()["command"], "second");
        server.await.unwrap();
    }

    #[tokio::test]
    async fn reconnects_after_truncation_without_replaying_mutations() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let connection =
            BlenderConnection::new("127.0.0.1".into(), listener.local_addr().unwrap().port());
        let server = tokio::spawn(async move {
            let (mut first, _) = listener.accept().await.unwrap();
            assert_eq!(read_request(&mut first).await["type"], "mutate");
            first.write_all(b"{\"status\":").await.unwrap();
            drop(first);
            let (mut next, _) = listener.accept().await.unwrap();
            assert_eq!(read_request(&mut next).await["type"], "inspect");
            next.write_all(br#"{"status":"success","result":{}}"#)
                .await
                .unwrap();
        });
        assert!(connection.send("mutate", json!({})).await.is_err());
        assert_eq!(
            connection.send("inspect", json!({})).await.unwrap(),
            json!({})
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn cancellation_discards_the_inflight_socket() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let connection =
            BlenderConnection::new("127.0.0.1".into(), listener.local_addr().unwrap().port());
        let (sent, received) = tokio::sync::oneshot::channel();
        let server = tokio::spawn(async move {
            let (mut first, _) = listener.accept().await.unwrap();
            read_request(&mut first).await;
            sent.send(()).unwrap();
            assert_eq!(first.read(&mut [0u8; 1]).await.unwrap(), 0);
            let (mut next, _) = listener.accept().await.unwrap();
            read_request(&mut next).await;
            next.write_all(br#"{"status":"success","result":{"fresh":true}}"#)
                .await
                .unwrap();
        });
        {
            let call = connection.send("slow", json!({}));
            tokio::pin!(call);
            tokio::select! { _ = &mut call => panic!("response was not sent"), _ = received => {} }
        }
        assert_eq!(
            connection.send("next", json!({})).await.unwrap()["fresh"],
            true
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn timeout_drops_connection_and_reports_uncertain_execution() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let mut connection =
            BlenderConnection::new("127.0.0.1".into(), listener.local_addr().unwrap().port());
        connection.deadline = Duration::from_millis(20);
        let server = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            read_request(&mut stream).await;
            assert_eq!(stream.read(&mut [0u8; 1]).await.unwrap(), 0);
        });
        let error = connection.send("slow", json!({})).await.unwrap_err();
        assert!(error.to_string().contains("may still run"));
        assert!(connection.stream.lock().await.is_none());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn invalid_and_oversized_responses_discard_connection() {
        for oversized in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let connection =
                BlenderConnection::new("127.0.0.1".into(), listener.local_addr().unwrap().port());
            let server = tokio::spawn(async move {
                let (mut stream, _) = listener.accept().await.unwrap();
                read_request(&mut stream).await;
                if oversized {
                    stream.write_all(b"{\"result\":\"").await.unwrap();
                    let chunk = vec![b'x'; 64 * 1024];
                    for _ in 0..=MAX_MESSAGE_BYTES / chunk.len() {
                        if stream.write_all(&chunk).await.is_err() {
                            break;
                        }
                    }
                } else {
                    stream.write_all(b"not-json").await.unwrap();
                }
            });
            let error = connection.send("inspect", json!({})).await.unwrap_err();
            assert!(
                error
                    .to_string()
                    .contains(if oversized { "32 MiB" } else { "Invalid JSON" })
            );
            assert!(connection.stream.lock().await.is_none());
            server.await.unwrap();
        }
    }
}
