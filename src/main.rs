use std::{io::IsTerminal, path::PathBuf, process::ExitCode, time::Duration};

use anyhow::Result;
use blender_mcp::{addon, connection::endpoint, doctor, security, server::BlenderServer};
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
    #[command(about = "Install the bundled extension using Blender 5.2 or later")]
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
    #[command(
        about = "Report connection, credentials, add-on, and Blender runtime status",
        after_help = "Exit status: 0 = ready (possibly with warnings), 1 = failed checks.\nRuntime details depend on the installed add-on version."
    )]
    Doctor {
        #[arg(long, help = "Output the report as JSON")]
        json: bool,
        #[arg(long, default_value_t = 5, value_parser = clap::value_parser!(u64).range(1..=300), help = "Timeout in seconds for each network check")]
        timeout: u64,
    },
}

#[tokio::main]
async fn main() -> Result<ExitCode> {
    match Cli::parse().command {
        Some(Command::PackageAddon { output }) => {
            println!("{}", addon::package(output)?.display());
        }
        Some(Command::InstallAddon { blender, repo }) => {
            let directory = security::directory()?;
            security::setup(&directory)?;
            addon::install(&blender, &repo)?;
            println!("Installed MCP for Blender {}.", env!("CARGO_PKG_VERSION"));
            println!("Enable MCP for Blender in Preferences > Add-ons.");
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
        Some(Command::Doctor { json, timeout }) => {
            let report = doctor::check(Duration::from_secs(timeout)).await;
            if json {
                println!("{}", serde_json::to_string_pretty(&report.json())?);
            } else {
                print!("{report}");
            }
            return Ok(if report.healthy() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            });
        }
        None => {
            let (host, port) = endpoint()?;
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
    Ok(ExitCode::SUCCESS)
}
