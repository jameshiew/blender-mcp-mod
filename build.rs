use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    io::Write,
    path::{Path, PathBuf},
};

use sha2::{Digest, Sha256};
use toml::Value;
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

fn main() {
    for path in ["Cargo.toml", "uv.lock", "extension", "LICENSE"] {
        println!("cargo:rerun-if-changed={path}");
    }
    let cargo = read_toml("Cargo.toml");
    let protocol = cargo["package"]["metadata"]["blender"]["protocol-version"]
        .as_integer()
        .filter(|version| *version > 0)
        .expect("protocol-version must be a positive integer");
    let version = env::var("CARGO_PKG_VERSION").unwrap();
    let extension = Path::new("extension");
    let mut manifest = read_toml(extension.join("blender_manifest.toml.in"));
    manifest.insert("version".into(), Value::String(version.clone()));
    let wheels = locked_wheels(extension);
    manifest.insert(
        "wheels".into(),
        Value::Array(
            wheels
                .keys()
                .map(|name| Value::String(format!("./{name}")))
                .collect(),
        ),
    );

    let mut files = wheels;
    add_python_sources(extension, extension, &mut files);
    files.insert(
        "blender_manifest.toml".into(),
        toml::to_string(&manifest).unwrap().into_bytes(),
    );
    files.insert(
        "protocol.json".into(),
        format!("{{\"version\":{protocol}}}\n").into_bytes(),
    );
    files.insert("LICENSE".into(), fs::read("LICENSE").unwrap());

    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    write_archive(&out.join("addon.zip"), files);
    let id = manifest["id"].as_str().unwrap();
    let blender_version_min = manifest["blender_version_min"].as_str().unwrap();
    fs::write(out.join("addon_metadata.rs"), format!(
        "pub const PROTOCOL_VERSION: u64 = {protocol};\npub const FILENAME: &str = \"{id}-{version}.zip\";\npub const BLENDER_VERSION_MIN: &str = {blender_version_min:?};\n"
    )).unwrap();
}

fn read_toml(path: impl AsRef<Path>) -> toml::Table {
    fs::read_to_string(path).unwrap().parse().unwrap()
}

fn locked_wheels(extension: &Path) -> BTreeMap<String, Vec<u8>> {
    let lock = read_toml("uv.lock");
    let packages = lock["package"].as_array().unwrap();
    let project = packages
        .iter()
        .find(|package| {
            package
                .get("source")
                .and_then(|source| source.get("virtual"))
                .and_then(Value::as_str)
                == Some(".")
        })
        .expect("Missing Python workspace in uv.lock");
    let mut pending: Vec<&Value> = project["dev-dependencies"]["addon"]
        .as_array()
        .unwrap()
        .iter()
        .collect();
    let mut visited = BTreeSet::new();
    let mut wheels = BTreeMap::new();
    while let Some(dependency) = pending.pop() {
        assert!(
            dependency.get("marker").is_none(),
            "Conditional wheel dependencies need platform resolution"
        );
        let name = dependency["name"].as_str().unwrap();
        if !visited.insert(name) {
            continue;
        }
        let matches: Vec<_> = packages
            .iter()
            .filter(|package| package["name"].as_str() == Some(name))
            .collect();
        assert_eq!(matches.len(), 1, "Expected one locked version of {name}");
        let package = matches[0];
        if let Some(dependencies) = package.get("dependencies") {
            pending.extend(dependencies.as_array().unwrap());
        }
        let wheel = package["wheels"]
            .as_array()
            .unwrap()
            .iter()
            .find(|wheel| {
                wheel["url"]
                    .as_str()
                    .unwrap()
                    .ends_with("-py3-none-any.whl")
            })
            .unwrap_or_else(|| panic!("No portable wheel for {name}"));
        let filename = wheel["url"].as_str().unwrap().rsplit('/').next().unwrap();
        let path = extension.join("wheels").join(filename);
        let data = fs::read(&path)
            .unwrap_or_else(|error| panic!("{}: {error}; run just sync-wheels", path.display()));
        assert_eq!(
            format!(
                "sha256:{}",
                Sha256::digest(&data)
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>()
            ),
            wheel["hash"].as_str().unwrap(),
            "Wheel checksum mismatch: {}; run just sync-wheels",
            path.display()
        );
        wheels.insert(format!("wheels/{filename}"), data);
    }
    wheels
}

fn add_python_sources(root: &Path, directory: &Path, files: &mut BTreeMap<String, Vec<u8>>) {
    for entry in fs::read_dir(directory).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name();
        if name.to_string_lossy().starts_with('.') || name == "__pycache__" || name == "wheels" {
            continue;
        }
        let path = entry.path();
        let kind = entry.file_type().unwrap();
        if kind.is_dir() {
            add_python_sources(root, &path, files);
        } else if kind.is_file() && path.extension().is_some_and(|extension| extension == "py") {
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .unwrap()
                .replace('\\', "/");
            files.insert(name, fs::read(path).unwrap());
        }
    }
}

fn write_archive(path: &Path, files: BTreeMap<String, Vec<u8>>) {
    let mut zip = ZipWriter::new(fs::File::create(path).unwrap());
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Stored)
        .unix_permissions(0o644);
    for (name, data) in files {
        zip.start_file(name, options).unwrap();
        zip.write_all(&data).unwrap();
    }
    zip.finish().unwrap();
}
