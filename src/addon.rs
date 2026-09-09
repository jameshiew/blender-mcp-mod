use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::Command,
};

use anyhow::{Context, Result, ensure};

include!(concat!(env!("OUT_DIR"), "/addon_metadata.rs"));
pub const PACKAGE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/addon.zip"));

pub fn package(output: Option<PathBuf>) -> Result<PathBuf> {
    let replace = output.is_none();
    let path = output.unwrap_or_else(|| Path::new("dist").join(FILENAME));
    if path.exists() && !replace {
        ensure!(
            fs::read(&path)? == PACKAGE,
            "{} already contains different data; choose another --output path",
            path.display()
        );
        return Ok(path);
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(PACKAGE)?;
    temporary.as_file().sync_all()?;
    if replace {
        temporary.persist(&path)
    } else {
        temporary.persist_noclobber(&path)
    }
    .with_context(|| format!("Cannot write {}", path.display()))?;
    Ok(path)
}

fn run(blender: &Path, arguments: &[&str]) -> Result<()> {
    let status = Command::new(blender)
        .args(["--disable-autoexec", "--command", "extension"])
        .args(arguments)
        .status()
        .with_context(|| {
            format!(
                "Cannot run {}; use --blender PATH to select Blender 5.0 or later",
                blender.display()
            )
        })?;
    ensure!(
        status.success(),
        "Blender extension command failed: {status}"
    );
    Ok(())
}

pub fn install(blender: &Path, repository: &str) -> Result<()> {
    let directory = tempfile::tempdir()?;
    let path = package(Some(directory.path().join(FILENAME)))?;
    run(
        blender,
        &[
            "install-file",
            "--repo",
            repository,
            path.to_str().context("Package path is not UTF-8")?,
        ],
    )
}

pub fn paths(blender: &Path) -> Result<()> {
    run(blender, &["repo-list"])?;
    run(blender, &["list"])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn export_is_idempotent_and_preserves_existing_files() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join(FILENAME);
        package(Some(target.clone())).unwrap();
        package(Some(target.clone())).unwrap();
        assert_eq!(fs::read(&target).unwrap(), PACKAGE);
        fs::write(&target, "local content").unwrap();
        assert!(package(Some(target.clone())).is_err());
        assert_eq!(fs::read_to_string(target).unwrap(), "local content");
    }

    #[cfg(unix)]
    #[test]
    fn export_preserves_dangling_symlinks() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join(FILENAME);
        let destination = directory.path().join("missing");
        std::os::unix::fs::symlink(&destination, &target).unwrap();
        assert!(package(Some(target.clone())).is_err());
        assert!(
            fs::symlink_metadata(target)
                .unwrap()
                .file_type()
                .is_symlink()
        );
        assert!(!destination.exists());
    }
}
