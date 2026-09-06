use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, bail, ensure};
use regex::Regex;
use rustpython_parser::{Parse, ast};

pub const PROTOCOL_VERSION: u64 = 7;
pub const SOURCE: &str = include_str!("../addon.py");
const FILENAME: &str = "blendermcp.py";

#[derive(Debug)]
struct AddonMetadata {
    version: Option<Vec<u64>>,
    build_version: Option<String>,
    protocol: Option<u64>,
}

impl AddonMetadata {
    fn release_version(&self) -> Result<Option<semver::Version>> {
        if let Some(version) = &self.build_version {
            return Ok(Some(semver::Version::parse(version).context(
                "Invalid ADDON_VERSION; leaving installation unchanged",
            )?));
        }
        let Some(version) = &self.version else {
            return Ok(None);
        };
        ensure!(
            version.len() <= 3,
            "Unsupported add-on version; leaving installation unchanged"
        );
        Ok(Some(semver::Version::new(
            version[0],
            *version.get(1).unwrap_or(&0),
            *version.get(2).unwrap_or(&0),
        )))
    }
}

fn string(expression: &ast::Expr) -> Option<&str> {
    if let ast::Expr::Constant(value) = expression
        && let ast::Constant::Str(value) = &value.value
    {
        return Some(value);
    }
    None
}

fn integer(expression: &ast::Expr) -> Option<u64> {
    if let ast::Expr::Constant(value) = expression
        && let ast::Constant::Int(value) = &value.value
    {
        return value.to_string().parse().ok();
    }
    None
}

fn metadata(source: &str) -> Option<AddonMetadata> {
    let statements = ast::Suite::parse(source, "<addon>").ok()?;
    let assignment = |name: &str| {
        statements.iter().rev().find_map(|statement| {
            if let ast::Stmt::Assign(assign) = statement
                && assign.targets.iter().any(|target| {
                    matches!(target, ast::Expr::Name(target) if target.id.as_str() == name)
                })
            {
                return Some(assign.value.as_ref());
            }
            None
        })
    };
    let ast::Expr::Dict(info) = assignment("bl_info")? else {
        return None;
    };
    if info.keys.iter().any(Option::is_none) {
        return None;
    }
    let field = |name: &str| {
        info.keys
            .iter()
            .zip(&info.values)
            .rev()
            .find_map(|(key, value)| (key.as_ref().and_then(string) == Some(name)).then_some(value))
    };
    if !matches!(
        string(field("name")?),
        Some("MCP for Blender" | "Blender MCP")
    ) {
        return None;
    }
    let version = match field("version") {
        Some(ast::Expr::Tuple(tuple)) if !tuple.elts.is_empty() => {
            Some(tuple.elts.iter().map(integer).collect::<Option<Vec<_>>>()?)
        }
        Some(_) => return None,
        None => None,
    };
    let build_version = match assignment("ADDON_VERSION") {
        Some(value) => Some(string(value)?.to_owned()),
        None => None,
    };
    let protocol = match assignment("ADDON_PROTOCOL_VERSION") {
        Some(value) => Some(integer(value)?),
        None => None,
    };
    Some(AddonMetadata {
        version,
        build_version,
        protocol,
    })
}

pub fn installed_version(path: &Path) -> Result<String> {
    let info = metadata(&fs::read_to_string(path)?)
        .context("Cannot read literal add-on version metadata")?;
    Ok(info.build_version.unwrap_or_else(|| {
        info.version.map_or_else(
            || "legacy (unversioned)".into(),
            |version| {
                version
                    .iter()
                    .map(u64::to_string)
                    .collect::<Vec<_>>()
                    .join(".")
            },
        )
    }))
}

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
    metadata(&text).is_some()
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

fn prepare_replacement(path: &Path) -> Result<Option<tempfile::NamedTempFile>> {
    if fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        bail!("Refusing to overwrite symlink {}", path.display());
    }
    if path.exists() {
        let old = fs::read(path).with_context(|| format!("Cannot read {}", path.display()))?;
        if old == SOURCE.as_bytes() {
            return Ok(None);
        }
        let installed = metadata(std::str::from_utf8(&old)?)
            .context("Refusing to replace an unrecognized add-on")?;
        let bundled = metadata(SOURCE).context("Invalid bundled add-on metadata")?;
        let bundled_version = bundled
            .release_version()?
            .context("Missing bundled version")?;
        ensure!(
            installed
                .release_version()?
                .is_none_or(|version| !version.cmp_precedence(&bundled_version).is_gt())
                && installed
                    .protocol
                    .is_none_or(|version| version <= PROTOCOL_VERSION),
            "{} is newer than this build; leaving it unchanged",
            path.display()
        );
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
    Ok(Some(temporary))
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
    let mut targets = Vec::new();
    for directory in &directories {
        targets = existing(std::slice::from_ref(directory))?;
        if !targets.is_empty() {
            break;
        }
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
    let prepared = targets
        .iter()
        .map(|target| prepare_replacement(target))
        .collect::<Result<Vec<_>>>()?;
    for (target, temporary) in targets.iter().zip(prepared) {
        if let Some(temporary) = temporary {
            temporary
                .persist(target)
                .with_context(|| format!("Cannot install add-on at {}", target.display()))?;
        }
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
    fn ignores_metadata_in_comments_strings_and_nested_scopes() {
        for source in [
            "# bl_info = {'name': 'Blender MCP'}\n",
            "example = \"bl_info = {'name': 'Blender MCP'}\"\n",
            "example = \"\"\"\nbl_info = {'name': 'Blender MCP'}\n\"\"\"\n",
            "def example():\n    bl_info = {'name': 'Blender MCP'}\n",
            "bl_info = {'description': {'name': 'Blender MCP'}, 'name': 'Other'}\n",
            "bl_info = {'description': \"'name': 'Blender MCP'\", 'name': 'Other'}\n",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let unrelated = directory.path().join("other.py");
            fs::write(&unrelated, source).unwrap();
            install(Some(directory.path().into())).unwrap();
            assert_eq!(fs::read_to_string(&unrelated).unwrap(), source);
            assert!(!unrelated.with_extension("py.bak").exists());
            assert!(directory.path().join(FILENAME).exists());
        }
    }

    #[test]
    fn updates_all_installations_in_the_selected_directory() {
        let directory = tempfile::tempdir().unwrap();
        let package = directory.path().join("a_package");
        fs::create_dir(&package).unwrap();
        let targets = [package.join("__init__.py"), directory.path().join(FILENAME)];
        for target in &targets {
            fs::write(target, "bl_info = {'name': 'Blender MCP'}").unwrap();
        }
        assert_eq!(install(Some(directory.path().into())).unwrap(), targets);
        for target in targets {
            assert_eq!(fs::read_to_string(target).unwrap(), SOURCE);
        }
    }

    #[test]
    fn embedded_addon_has_matching_protocol_and_no_recording() {
        let info = metadata(SOURCE).unwrap();
        assert_eq!(info.protocol, Some(PROTOCOL_VERSION));
        assert_eq!(
            info.build_version.as_deref(),
            Some(env!("CARGO_PKG_VERSION"))
        );
        assert_eq!(
            info.version,
            Some(vec![
                env!("CARGO_PKG_VERSION_MAJOR").parse().unwrap(),
                env!("CARGO_PKG_VERSION_MINOR").parse().unwrap(),
                env!("CARGO_PKG_VERSION_PATCH").parse().unwrap(),
            ])
        );
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

    #[test]
    fn refuses_newer_versions_before_replacing_any_installation() {
        for newer in [
            "bl_info = {'name': 'Blender MCP', 'version': (2, 0, 0)}",
            "bl_info = {'name': 'Blender MCP', 'version': (1, 6)}\nADDON_VERSION = '2.0.0+mod'",
            "bl_info = {'name': 'Blender MCP', 'version': (1, 9, 1)}\nADDON_PROTOCOL_VERSION = 8",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let older = directory.path().join("a.py");
            let target = directory.path().join(FILENAME);
            let previous = "bl_info = {'name': 'Blender MCP', 'version': (1, 6)}";
            fs::write(&older, previous).unwrap();
            fs::write(&target, newer).unwrap();
            let error = install(Some(directory.path().into())).unwrap_err();
            assert!(error.to_string().contains("newer"));
            assert_eq!(fs::read_to_string(&older).unwrap(), previous);
            assert_eq!(fs::read_to_string(&target).unwrap(), newer);
        }
    }

    #[test]
    fn reports_full_build_version_and_legacy_numeric_version() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join(FILENAME);
        fs::write(
            &target,
            "bl_info = {'name': 'Blender MCP', 'version': (1, 6)}",
        )
        .unwrap();
        assert_eq!(installed_version(&target).unwrap(), "1.6");
        install(Some(directory.path().into())).unwrap();
        assert_eq!(installed_version(&target).unwrap(), "1.9.1+mod");
    }

    #[test]
    fn compares_release_precedence_without_ordering_build_labels() {
        for version in ["1.9.1+upstream", "1.9.1-alpha+mod", "1.9.0+mod"] {
            let directory = tempfile::tempdir().unwrap();
            let target = directory.path().join(FILENAME);
            fs::write(
                &target,
                format!("bl_info = {{'name': 'Blender MCP'}}\nADDON_VERSION = '{version}'"),
            )
            .unwrap();
            install(Some(directory.path().into())).unwrap();
            assert_eq!(installed_version(&target).unwrap(), "1.9.1+mod");
        }
    }
}
