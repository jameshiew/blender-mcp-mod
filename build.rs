use std::{collections::BTreeSet, env, fs, io::Write, path::PathBuf};

use sha2::{Digest, Sha256};
use toml::Value;
use zip::{CompressionMethod, ZipWriter, write::SimpleFileOptions};

fn main() {
    for path in ["Cargo.toml", "uv.lock", "extension", "LICENSE"] {
        println!("cargo:rerun-if-changed={path}");
    }
    let cargo: toml::Table = fs::read_to_string("Cargo.toml").unwrap().parse().unwrap();
    let protocol = cargo["package"]["metadata"]["blender"]["protocol-version"]
        .as_integer()
        .filter(|version| *version > 0)
        .expect("protocol-version must be a positive integer");
    let version = env::var("CARGO_PKG_VERSION").unwrap();
    let mut manifest: toml::Table = fs::read_to_string("extension/blender_manifest.toml.in")
        .unwrap()
        .parse()
        .unwrap();
    manifest.insert("version".into(), Value::String(version.clone()));

    let lock: toml::Table = fs::read_to_string("uv.lock").unwrap().parse().unwrap();
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
    let mut wheels = Vec::new();
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
        let path = format!("extension/wheels/{filename}");
        let data =
            fs::read(&path).unwrap_or_else(|error| panic!("{path}: {error}; run just sync-wheels"));
        assert_eq!(
            format!(
                "sha256:{}",
                Sha256::digest(&data)
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>()
            ),
            wheel["hash"].as_str().unwrap(),
            "Wheel checksum mismatch: {path}; run just sync-wheels"
        );
        wheels.push((format!("wheels/{filename}"), data));
    }
    wheels.sort_by(|a, b| a.0.cmp(&b.0));
    manifest.insert(
        "wheels".into(),
        Value::Array(
            wheels
                .iter()
                .map(|(name, _)| Value::String(format!("./{name}")))
                .collect(),
        ),
    );

    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let mut zip = ZipWriter::new(fs::File::create(out.join("addon.zip")).unwrap());
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Stored)
        .unix_permissions(0o644);
    for (name, data) in [
        (
            "__init__.py".into(),
            fs::read("extension/__init__.py").unwrap(),
        ),
        (
            "blender_manifest.toml".into(),
            toml::to_string(&manifest).unwrap().into_bytes(),
        ),
        (
            "protocol.json".into(),
            format!("{{\"version\":{protocol}}}\n").into_bytes(),
        ),
        ("LICENSE".into(), fs::read("LICENSE").unwrap()),
    ]
    .into_iter()
    .chain(wheels)
    {
        zip.start_file(name, options).unwrap();
        zip.write_all(&data).unwrap();
    }
    zip.finish().unwrap();
    let id = manifest["id"].as_str().unwrap();
    fs::write(out.join("addon_metadata.rs"), format!(
        "pub const PROTOCOL_VERSION: u64 = {protocol};\npub const FILENAME: &str = \"{id}-{version}.zip\";\n"
    )).unwrap();
}
