use std::{io::IsTerminal, path::PathBuf};

use anyhow::{Context, Result};
use blender_mcp::{addon, server::BlenderServer, tools};
use clap::{Parser, Subcommand};
use rmcp::ServiceExt;

#[derive(Parser)]
#[command(
    version,
    about = "MCP for Blender: stdio server and add-on installer",
    after_help = "Without a subcommand, serve MCP on stdin/stdout.\nBLENDER_HOST=localhost BLENDER_PORT=9876\nBLENDER_MCP_SAFE_MODE=1 enables the Python AST guard inside Blender."
)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    #[command(about = "Install the embedded add-on, preserving existing files in backups")]
    InstallAddon {
        #[arg(long)]
        addons_dir: Option<PathBuf>,
    },
    #[command(about = "List discovered Blender add-on directories and installations")]
    AddonPaths,
}

#[tokio::main]
async fn main() -> Result<()> {
    match Cli::parse().command {
        Some(Command::InstallAddon { addons_dir }) => {
            for path in addon::install(addons_dir)? {
                println!("Installed {}", path.display());
            }
            println!("Restart Blender or disable and enable the add-on, then Start MCP Server.");
        }
        Some(Command::AddonPaths) => {
            let directories = addon::discover()?;
            for path in &directories {
                println!(
                    "{} ({})",
                    path.display(),
                    if path.is_dir() { "exists" } else { "missing" }
                );
            }
            for path in addon::existing(&directories)? {
                println!("Installed: {}", path.display());
            }
            anyhow::ensure!(
                !directories.is_empty(),
                "No Blender add-on directories found"
            );
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
            if let Err(error) = addon::check_installed() {
                eprintln!("Could not check local add-on: {error}");
            }
            let service = BlenderServer::new(host, port, tools::safe_mode_enabled())?
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
