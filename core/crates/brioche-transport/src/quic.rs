//! QUIC (quinn + rustls) reliable multiplexed control/input/cursor/file plane
//! with SHA-256 certificate pinning (no public CA required).

use std::net::SocketAddr;
use std::sync::Arc;

use brioche_proto::ControlMessage;
pub use quinn::Connection as QuicConnection;
use quinn::{ClientConfig, Connection, Endpoint, ServerConfig};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer, ServerName, UnixTime};
use rustls::{DigitallySignedStruct, Error as TlsError, SignatureScheme};
use sha2::{Digest, Sha256};
use thiserror::Error;

const MAX_CONTROL_FRAME: usize = 256 * 1024;
const ALPN_BRIOCHE_V3: &[u8] = b"brioche-ctrl/3";

#[derive(Debug, Error)]
pub enum QuicError {
    #[error("certificate generation failed: {0}")]
    Cert(String),
    #[error("tls config failed: {0}")]
    Tls(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("quic connect failed: {0}")]
    Connect(String),
    #[error("control frame too large: {0}")]
    FrameTooLarge(usize),
    #[error("serialization failed: {0}")]
    Serde(#[from] serde_json::Error),
}

pub struct PinnedIdentity {
    pub cert_der: CertificateDer<'static>,
    pub key_der: PrivateKeyDer<'static>,
    pub sha256_pin: String,
}

pub fn generate_self_signed_identity() -> Result<PinnedIdentity, QuicError> {
    let cert = rcgen::generate_simple_self_signed(vec!["brioche.local".to_string()])
        .map_err(|e| QuicError::Cert(e.to_string()))?;
    let cert_der = cert.cert.der().clone();
    let key_der = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(cert.key_pair.serialize_der()));
    let pin = hex_sha256(cert_der.as_ref());
    Ok(PinnedIdentity {
        cert_der,
        key_der,
        sha256_pin: pin,
    })
}

pub fn hex_sha256(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

#[derive(Debug)]
struct PinnedCertVerifier {
    expected_pin: String,
    crypto_provider: Arc<rustls::crypto::CryptoProvider>,
}

impl ServerCertVerifier for PinnedCertVerifier {
    fn verify_server_cert(
        &self,
        end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, TlsError> {
        let actual = hex_sha256(end_entity.as_ref());
        if actual.eq_ignore_ascii_case(&self.expected_pin) {
            Ok(ServerCertVerified::assertion())
        } else {
            Err(TlsError::General(format!(
                "certificate pin mismatch: expected {}, got {}",
                self.expected_pin, actual
            )))
        }
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        rustls::crypto::verify_tls12_signature(
            message,
            cert,
            dss,
            &self.crypto_provider.signature_verification_algorithms,
        )
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, TlsError> {
        rustls::crypto::verify_tls13_signature(
            message,
            cert,
            dss,
            &self.crypto_provider.signature_verification_algorithms,
        )
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.crypto_provider
            .signature_verification_algorithms
            .supported_schemes()
    }
}

pub fn bind_quic_server(
    addr: SocketAddr,
    id: &PinnedIdentity,
) -> Result<(Endpoint, SocketAddr), QuicError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut tls = rustls::ServerConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|e| QuicError::Tls(e.to_string()))?
        .with_no_client_auth()
        .with_single_cert(vec![id.cert_der.clone()], id.key_der.clone_key())
        .map_err(|e| QuicError::Tls(e.to_string()))?;
    tls.alpn_protocols = vec![ALPN_BRIOCHE_V3.to_vec()];

    let quic_crypto = quinn::crypto::rustls::QuicServerConfig::try_from(tls)
        .map_err(|e| QuicError::Tls(e.to_string()))?;
    let mut server_cfg = ServerConfig::with_crypto(Arc::new(quic_crypto));
    let mut transport = quinn::TransportConfig::default();
    transport.max_idle_timeout(Some(std::time::Duration::from_secs(45).try_into().unwrap()));
    transport.keep_alive_interval(Some(std::time::Duration::from_secs(4)));
    server_cfg.transport_config(Arc::new(transport));

    let endpoint = Endpoint::server(server_cfg, addr)?;
    let local = endpoint.local_addr()?;
    Ok((endpoint, local))
}

pub async fn connect_quic_client(
    addr: SocketAddr,
    expected_pin: &str,
) -> Result<(Endpoint, Connection), QuicError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let verifier = Arc::new(PinnedCertVerifier {
        expected_pin: expected_pin.to_string(),
        crypto_provider: provider.clone(),
    });
    let mut tls = rustls::ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|e| QuicError::Tls(e.to_string()))?
        .dangerous()
        .with_custom_certificate_verifier(verifier)
        .with_no_client_auth();
    tls.alpn_protocols = vec![ALPN_BRIOCHE_V3.to_vec()];

    let quic_crypto = quinn::crypto::rustls::QuicClientConfig::try_from(tls)
        .map_err(|e| QuicError::Tls(e.to_string()))?;
    let mut client_cfg = ClientConfig::new(Arc::new(quic_crypto));
    let mut transport = quinn::TransportConfig::default();
    transport.max_idle_timeout(Some(std::time::Duration::from_secs(45).try_into().unwrap()));
    transport.keep_alive_interval(Some(std::time::Duration::from_secs(4)));
    client_cfg.transport_config(Arc::new(transport));

    let bind_addr: SocketAddr = if addr.is_ipv6() {
        "[::]:0".parse().unwrap()
    } else {
        "0.0.0.0:0".parse().unwrap()
    };
    let mut endpoint = Endpoint::client(bind_addr)?;
    endpoint.set_default_client_config(client_cfg);
    let conn = endpoint
        .connect(addr, "brioche.local")
        .map_err(|e| QuicError::Connect(e.to_string()))?
        .await
        .map_err(|e| QuicError::Connect(e.to_string()))?;
    Ok((endpoint, conn))
}

pub async fn send_control(
    send: &mut quinn::SendStream,
    msg: &ControlMessage,
) -> Result<(), QuicError> {
    let bytes = serde_json::to_vec(msg)?;
    if bytes.len() > MAX_CONTROL_FRAME {
        return Err(QuicError::FrameTooLarge(bytes.len()));
    }
    send.write_all(&(bytes.len() as u32).to_be_bytes())
        .await
        .map_err(|e| QuicError::Connect(e.to_string()))?;
    send.write_all(&bytes)
        .await
        .map_err(|e| QuicError::Connect(e.to_string()))?;
    Ok(())
}

pub async fn recv_control(recv: &mut quinn::RecvStream) -> Result<ControlMessage, QuicError> {
    let mut len_buf = [0u8; 4];
    recv.read_exact(&mut len_buf)
        .await
        .map_err(|e| QuicError::Connect(e.to_string()))?;
    let len = u32::from_be_bytes(len_buf) as usize;
    if len == 0 || len > MAX_CONTROL_FRAME {
        return Err(QuicError::FrameTooLarge(len));
    }
    let mut buf = vec![0u8; len];
    recv.read_exact(&mut buf)
        .await
        .map_err(|e| QuicError::Connect(e.to_string()))?;
    Ok(serde_json::from_slice(&buf)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn quic_pinned_control_stream_roundtrip_and_wrong_pin_rejected() {
        let id = generate_self_signed_identity().unwrap();
        let (server_ep, addr) = bind_quic_server("127.0.0.1:0".parse().unwrap(), &id).unwrap();

        let server_ep_clone = server_ep.clone();
        let server_task = tokio::spawn(async move {
            let incoming = server_ep_clone.accept().await.unwrap();
            let conn = incoming.await.unwrap();
            let (mut send, mut recv) = conn.accept_bi().await.unwrap();
            let msg = recv_control(&mut recv).await.unwrap();
            send_control(&mut send, &msg).await.unwrap();
            send.finish().unwrap();
            let _ = send.stopped().await;
            msg
        });

        let (_client_ep, conn) = connect_quic_client(addr, &id.sha256_pin).await.unwrap();
        let (mut send, mut recv) = conn.open_bi().await.unwrap();
        let sent = ControlMessage::SwitchMonitor {
            monitor_id: 2,
            request_id: 7,
        };
        send_control(&mut send, &sent).await.unwrap();
        let echoed = recv_control(&mut recv).await.unwrap();
        assert_eq!(echoed, sent);
        conn.close(0u32.into(), b"done");
        assert_eq!(server_task.await.unwrap(), sent);

        // Wrong pin must be rejected during TLS 1.3 handshake
        let wrong_pin = "0".repeat(64);
        let server_ep_reject = server_ep.clone();
        let reject_task = tokio::spawn(async move {
            if let Some(inc) = server_ep_reject.accept().await {
                let _ = inc.await;
            }
        });
        assert!(connect_quic_client(addr, &wrong_pin).await.is_err());
        reject_task.abort();
        drop(server_ep);
    }
}
