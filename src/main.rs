use std::{io::IsTerminal, path::PathBuf};

use anyhow::{Context, Result};
use blender_mcp::{addon, security, server::BlenderServer};
use clap::{Parser, Subcommand};
use rmcp::ServiceExt;

#[derive(Parser)]
#[command(
    version,
    about = "MCP for Blender: stdio server and add-on installer",
    after_help = "Without a subcommand, serve MCP on stdin/stdout.\nBLENDER_HOST=localhost BLENDER_PORT=9876\nConnections require mutual TLS. Run install-addon or setup-connection first.\nBLENDER_MCP_CONFIG_DIR overrides the credential directory (default: ~/.blender-mcp)."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    #[command(about = "Export the bundled Blender extension ZIP for Install from Disk")]
    PackageAddon {
        #[arg(long)]
        output: Option<PathBuf>,
    },
    #[command(about = "Install the bundled extension using Blender 4.2 or later")]
    InstallAddon {
        #[arg(long, default_value = "blender")]
        blender: PathBuf,
        #[arg(long, default_value = "user_default")]
        repo: String,
    },
    #[command(about = "List Blender extension repositories and installed extensions")]
    AddonPaths {
        #[arg(long, default_value = "blender")]
        blender: PathBuf,
    },
    #[command(about = "Create local TLS credentials without replacing an existing pairing")]
    SetupConnection,
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
        Some(Command::PackageAddon { output }) => {
            println!("{}", addon::package(output)?.display());
        }
        Some(Command::InstallAddon { blender, repo }) => {
            let directory = security::directory()?;
            security::setup(&directory)?;
            addon::install(&blender, &repo)?;
            println!("Installed MCP for Blender {}.", env!("CARGO_PKG_VERSION"));
            println!(
                "Disable any legacy MCP add-on, then enable MCP for Blender in Preferences > Add-ons."
            );
        }
        Some(Command::SetupConnection) => {
            let directory = security::directory()?;
            security::setup(&directory)?;
            println!("Connection credentials ready in {}", directory.display());
            println!("Restart Blender or start the MCP add-on to use them.");
        }
        Some(Command::AddonPaths { blender }) => {
            addon::paths(&blender)?;
        }
        None => {
            let host = std::env::var("BLENDER_HOST").unwrap_or_else(|_| "localhost".into());
            let port = std::env::var("BLENDER_PORT")
                .unwrap_or_else(|_| "9876".into())
                .parse()
                .context("BLENDER_PORT must be a port number")?;
            anyhow::ensure!(port != 0, "BLENDER_PORT must be between 1 and 65535");
            if std::io::stdin().is_terminal() {
                eprintln!(
                    "Waiting for an MCP client on stdin. Configure your client to launch blender-mcp; Ctrl-C exits."
                );
            }
            let service = BlenderServer::new(host, port)?
                .serve(rmcp::transport::stdio())
                .await?;
            tokio::select! {
                result = service.waiting() => { result?; }
                result = tokio::signal::ctrl_c() => { result?; }
            }
        }
    }
    Ok(())
}
