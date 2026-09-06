use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::{Context, Result, ensure};
use p256::{
    ecdsa::{Signature, SigningKey, signature::Signer},
    pkcs8::{EncodePrivateKey, LineEnding},
};
use rcgen::{
    BasicConstraints, CertificateParams, CertifiedIssuer, ExtendedKeyUsagePurpose, IsCa,
    KeyIdMethod, KeyUsagePurpose, PublicKeyData,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio_rustls::rustls::{
    self, ClientConfig, RootCertStore,
    pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject},
};

pub const SERVER_NAME: &str = "blender-mcp.local";

pub fn directory() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("BLENDER_MCP_CONFIG_DIR") {
        ensure!(!path.is_empty(), "BLENDER_MCP_CONFIG_DIR must not be empty");
        let path = PathBuf::from(path);
        ensure!(
            path.is_absolute(),
            "BLENDER_MCP_CONFIG_DIR must be an absolute path"
        );
        return Ok(path);
    }
    Ok(dirs::home_dir()
        .context("Cannot determine home directory")?
        .join(".blender-mcp"))
}

fn check_private(path: &Path, directory: bool) -> Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    ensure!(
        if directory {
            metadata.is_dir()
        } else {
            metadata.is_file()
        },
        "{} must be a regular {} (no symlinks)",
        path.display(),
        if directory { "directory" } else { "file" }
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            metadata.uid() == rustix::process::geteuid().as_raw() && metadata.mode() & 0o077 == 0,
            "{} must be owned by the current user with no group/other permissions",
            path.display()
        );
    }
    Ok(())
}

pub fn load(directory: &Path) -> Result<Value> {
    check_private(directory, true)?;
    let path = directory.join("credentials.json");
    check_private(&path, false)?;
    let data: Value = serde_json::from_slice(&fs::read(path)?)?;
    ensure!(
        data["version"] == 1,
        "Unsupported Blender connection credentials"
    );
    for field in [
        "ca",
        "server_cert",
        "server_key",
        "client_cert",
        "client_key",
    ] {
        ensure!(
            data[field].as_str().is_some_and(|v| !v.is_empty()),
            "Missing Blender credential field: {field}"
        );
    }
    Ok(data)
}

pub fn setup(directory: &Path) -> Result<()> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    match builder.create(directory) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error).context("Cannot create Blender credential directory"),
    }
    check_private(directory, true)?;
    let path = directory.join("credentials.json");
    match fs::symlink_metadata(&path) {
        Ok(_) => {
            validate_setup(&load(directory)?)
                .context("Existing Blender credentials are invalid; left unchanged")?;
            return Ok(());
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    let data = generate()?;
    let mut temporary = tempfile::NamedTempFile::new_in(directory)?;
    temporary.write_all(&serde_json::to_vec_pretty(&data)?)?;
    temporary.as_file().sync_all()?;
    match temporary.persist_noclobber(path) {
        Ok(_) => {}
        Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error).context("Cannot save Blender credentials"),
    }
    validate_setup(&load(directory)?)?;
    Ok(())
}

fn validate_setup(data: &Value) -> Result<()> {
    client_config(data)?;
    let cert = CertificateDer::from_pem_slice(
        data["server_cert"]
            .as_str()
            .context("Missing server certificate")?
            .as_bytes(),
    )
    .context("Invalid server certificate")?;
    let key = PrivateKeyDer::from_pem_slice(
        data["server_key"]
            .as_str()
            .context("Missing server key")?
            .as_bytes(),
    )
    .context("Invalid server key")?;
    rustls::sign::CertifiedKey::from_der(vec![cert], key, &rustls_rustcrypto::provider())
        .context("Invalid server certificate/key pair")?;
    Ok(())
}

struct CertificateKey {
    key: SigningKey,
    public: Vec<u8>,
}

impl CertificateKey {
    fn generate() -> Result<Self> {
        loop {
            let mut bytes = [0u8; 32];
            rustls_rustcrypto::provider()
                .secure_random
                .fill(&mut bytes)
                .map_err(|_| anyhow::anyhow!("Operating system random generator failed"))?;
            if let Ok(key) = SigningKey::from_slice(&bytes) {
                let public = key.verifying_key().to_sec1_point(false).as_bytes().to_vec();
                return Ok(Self { key, public });
            }
        }
    }

    fn pem(&self) -> Result<String> {
        Ok(self
            .key
            .to_pkcs8_pem(LineEnding::LF)
            .map_err(|_| anyhow::anyhow!("Cannot encode connection key"))?
            .to_string())
    }
}

impl PublicKeyData for CertificateKey {
    fn der_bytes(&self) -> &[u8] {
        &self.public
    }
    fn algorithm(&self) -> &'static rcgen::SignatureAlgorithm {
        &rcgen::PKCS_ECDSA_P256_SHA256
    }
}

impl rcgen::SigningKey for CertificateKey {
    fn sign(&self, message: &[u8]) -> std::result::Result<Vec<u8>, rcgen::Error> {
        let signature: Signature = self.key.sign(message);
        Ok(signature.to_der().as_bytes().to_vec())
    }
}

fn parameters(name: &str, key: &CertificateKey) -> Result<CertificateParams> {
    let mut params = CertificateParams::new(vec![name.into()])?;
    let mut serial = [0u8; 16];
    let provider = rustls_rustcrypto::provider();
    provider
        .secure_random
        .fill(&mut serial)
        .map_err(|_| anyhow::anyhow!("Operating system random generator failed"))?;
    params.serial_number = Some(rcgen::SerialNumber::from_slice(&serial));
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(rcgen::DnType::CommonName, name);
    params.key_identifier_method = KeyIdMethod::PreSpecified(Sha256::digest(&key.public).to_vec());
    params.use_authority_key_identifier_extension = true;
    params.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    Ok(params)
}

fn generate() -> Result<Value> {
    let ca_key = CertificateKey::generate()?;
    let mut ca_params = parameters("blender-mcp-pairing.local", &ca_key)?;
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Constrained(0));
    ca_params.key_usages = vec![KeyUsagePurpose::KeyCertSign];
    let ca = CertifiedIssuer::self_signed(ca_params, ca_key)?;
    let server_key = CertificateKey::generate()?;
    let mut server_params = parameters(SERVER_NAME, &server_key)?;
    server_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    let server_cert = server_params.signed_by(&server_key, &ca)?;
    let client_key = CertificateKey::generate()?;
    let mut client_params = parameters("blender-mcp-client.local", &client_key)?;
    client_params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
    let client_cert = client_params.signed_by(&client_key, &ca)?;
    Ok(
        json!({"version": 1, "ca": ca.pem(), "server_cert": server_cert.pem(), "server_key": server_key.pem()?, "client_cert": client_cert.pem(), "client_key": client_key.pem()?}),
    )
}

pub fn client_config(data: &Value) -> Result<Arc<ClientConfig>> {
    let mut roots = RootCertStore::empty();
    roots.add(CertificateDer::from_pem_slice(
        data["ca"].as_str().context("Missing CA")?.as_bytes(),
    )?)?;
    let cert = CertificateDer::from_pem_slice(
        data["client_cert"]
            .as_str()
            .context("Missing client certificate")?
            .as_bytes(),
    )?;
    let key = PrivateKeyDer::from_pem_slice(
        data["client_key"]
            .as_str()
            .context("Missing client key")?
            .as_bytes(),
    )?;
    Ok(Arc::new(
        ClientConfig::builder_with_provider(Arc::new(rustls_rustcrypto::provider()))
            .with_protocol_versions(&[&rustls::version::TLS13])?
            .with_root_certificates(roots)
            .with_client_auth_cert(vec![cert], key)?,
    ))
}

#[cfg(test)]
pub(crate) fn test_config() -> (Arc<ClientConfig>, Arc<rustls::ServerConfig>) {
    let data = generate().unwrap();
    let mut roots = RootCertStore::empty();
    roots
        .add(CertificateDer::from_pem_slice(data["ca"].as_str().unwrap().as_bytes()).unwrap())
        .unwrap();
    let provider = Arc::new(rustls_rustcrypto::provider());
    let verifier = rustls::server::WebPkiClientVerifier::builder_with_provider(
        Arc::new(roots),
        provider.clone(),
    )
    .build()
    .unwrap();
    let config = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .unwrap()
        .with_client_cert_verifier(verifier)
        .with_single_cert(
            vec![
                CertificateDer::from_pem_slice(data["server_cert"].as_str().unwrap().as_bytes())
                    .unwrap(),
            ],
            PrivateKeyDer::from_pem_slice(data["server_key"].as_str().unwrap().as_bytes()).unwrap(),
        )
        .unwrap();
    (client_config(&data).unwrap(), Arc::new(config))
}
