use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail, ensure};
use regex::Regex;

pub const PROTOCOL_VERSION: u64 = 6;
pub const SOURCE: &str = include_str!("../addon.py");
const FILENAME: &str = "blendermcp.py";

fn expand_home(path: PathBuf) -> Result<PathBuf> {
    if path == Path::new("~") {
        return dirs::home_dir().context("Cannot determine home directory");
    }
    if let Ok(suffix) = path.strip_prefix("~/") {
        return Ok(dirs::home_dir()
            .context("Cannot determine home directory")?
            .join(suffix));
    }
    Ok(path)
}

pub fn discover() -> Result<Vec<PathBuf>> {
    let mut result = Vec::new();
    if let Some(path) = std::env::var_os("BLENDER_USER_ADDONS")
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var_os("BLENDERMCP_ADDONS_DIR").filter(|v| !v.is_empty()))
    {
        result.push(expand_home(PathBuf::from(path))?);
    }
    let home = dirs::home_dir().context("Cannot determine home directory")?;
    let base = if cfg!(target_os = "macos") {
        home.join("Library/Application Support/Blender")
    } else if cfg!(windows) {
        dirs::config_dir()
            .context("Cannot determine application data directory")?
            .join("Blender Foundation/Blender")
    } else {
        dirs::config_dir()
            .unwrap_or_else(|| home.join(".config"))
            .join("blender")
    };
    if base.is_dir() {
        let mut versions = fs::read_dir(base)?.collect::<std::io::Result<Vec<_>>>()?;
        versions.sort_by_key(|entry| std::cmp::Reverse(entry.file_name()));
        let version = Regex::new(r"^\d+\.\d+")?;
        for entry in versions {
            if entry.file_type()?.is_dir() && version.is_match(&entry.file_name().to_string_lossy())
            {
                result.push(entry.path().join("scripts/addons"));
                let extensions = entry.path().join("extensions/user_default");
                if extensions.is_dir() {
                    result.push(extensions);
                }
            }
        }
    }
    let mut unique = Vec::new();
    for path in result {
        if !unique.contains(&path) {
            unique.push(path);
        }
    }
    Ok(unique)
}

fn is_addon(path: &Path) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    Regex::new(
        r#"(?s)\bbl_info\s*=\s*\{[^}]*["']name["']\s*:\s*["'](?:MCP for Blender|Blender MCP)["']"#,
    )
    .expect("constant regex")
    .is_match(&text)
}

pub fn existing(dirs: &[PathBuf]) -> Result<Vec<PathBuf>> {
    let mut found = Vec::new();
    for directory in dirs {
        if !directory.is_dir() {
            continue;
        }
        let mut entries = fs::read_dir(directory)?.collect::<std::io::Result<Vec<_>>>()?;
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let path = entry.path();
            let candidate = if path.is_dir() {
                path.join("__init__.py")
            } else {
                path
            };
            if candidate
                .extension()
                .is_some_and(|extension| extension == "py")
                && is_addon(&candidate)
            {
                found.push(candidate);
            }
        }
    }
    Ok(found)
}

fn replace(path: &Path) -> Result<()> {
    if fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        bail!("Refusing to overwrite symlink {}", path.display());
    }
    if path.exists() {
        let old = fs::read(path).with_context(|| format!("Cannot read {}", path.display()))?;
        if old == SOURCE.as_bytes() {
            return Ok(());
        }
        let mut suffix = 0;
        loop {
            let backup = path.with_extension(if suffix == 0 {
                "py.bak".into()
            } else {
                format!("py.bak.{suffix}")
            });
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&backup)
            {
                Ok(mut file) => {
                    file.write_all(&old)
                        .context("Cannot back up existing add-on; leaving it unchanged")?;
                    file.sync_all()?;
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => suffix += 1,
                Err(error) => {
                    return Err(error)
                        .context("Cannot create add-on backup; leaving installation unchanged");
                }
            }
        }
    }
    let parent = path
        .parent()
        .context("Add-on path needs a parent directory")?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(SOURCE.as_bytes())?;
    temporary.as_file().sync_all()?;
    temporary
        .persist(path)
        .with_context(|| format!("Cannot install add-on at {}", path.display()))?;
    Ok(())
}

pub fn install(directory: Option<PathBuf>) -> Result<Vec<PathBuf>> {
    let directories = match directory {
        Some(path) => vec![expand_home(path)?],
        None => discover()?,
    };
    ensure!(
        !directories.is_empty(),
        "No Blender add-on directory found; use install-addon --addons-dir PATH"
    );
    let mut targets = existing(&directories)?;
    if let Some(first) = targets.first().cloned() {
        targets.retain(|path| path.parent() == first.parent());
    }
    if targets.is_empty() {
        let directory = &directories[0];
        fs::create_dir_all(directory)?;
        let target = directory.join(FILENAME);
        ensure!(
            !target.exists(),
            "{} exists but is not a recognized MCP add-on; choose another directory",
            target.display()
        );
        targets.push(target);
    }
    for target in &targets {
        replace(target)?;
    }
    Ok(targets)
}

pub fn check_installed() -> Result<()> {
    let installs = existing(&discover()?)?;
    if installs.is_empty() {
        eprintln!(
            "No local MCP add-on found. Use blender-mcp install-addon, or install addon.py in Blender."
        );
    } else {
        for path in installs {
            if fs::read(&path)? != SOURCE.as_bytes() {
                eprintln!(
                    "Add-on differs from this build: {}. Use blender-mcp install-addon and restart Blender to update.",
                    path.display()
                );
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_preserves_each_previous_version_and_is_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join(FILENAME);
        let previous = "bl_info = {'name': 'MCP for Blender'}\n# local edits\n";
        fs::write(&target, previous).unwrap();
        install(Some(directory.path().into())).unwrap();
        install(Some(directory.path().into())).unwrap();
        assert_eq!(
            fs::read_to_string(target.with_extension("py.bak")).unwrap(),
            previous
        );
        assert!(!target.with_extension("py.bak.1").exists());
        let newer = format!("{previous}# more edits\n");
        fs::write(&target, &newer).unwrap();
        install(Some(directory.path().into())).unwrap();
        assert_eq!(
            fs::read_to_string(target.with_extension("py.bak.1")).unwrap(),
            newer
        );
        assert_eq!(fs::read_to_string(target).unwrap(), SOURCE);
    }

    #[test]
    fn updates_extension_package_without_creating_duplicate_addon() {
        let directory = tempfile::tempdir().unwrap();
        let package = directory.path().join("mcp_extension");
        fs::create_dir(&package).unwrap();
        fs::write(
            package.join("__init__.py"),
            "bl_info = {'name': 'Blender MCP'}",
        )
        .unwrap();
        let paths = install(Some(directory.path().into())).unwrap();
        assert_eq!(paths, vec![package.join("__init__.py")]);
        assert!(!directory.path().join(FILENAME).exists());
    }

    #[test]
    fn refuses_unrecognized_files_and_keeps_invalid_bytes() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join(FILENAME);
        fs::write(&target, [0xff, 0x00]).unwrap();
        assert!(install(Some(directory.path().into())).is_err());
        assert_eq!(fs::read(target).unwrap(), [0xff, 0x00]);
    }

    #[test]
    fn embedded_addon_has_matching_protocol_and_no_recording() {
        assert!(SOURCE.contains(&format!("ADDON_PROTOCOL_VERSION = {PROTOCOL_VERSION}")));
        for removed in [
            "telemetry",
            "trajectory",
            "UserEditRecorder",
            "uuid.getnode",
            "drain_human_activity",
        ] {
            assert!(!SOURCE.contains(removed), "{removed}");
        }
    }
}
