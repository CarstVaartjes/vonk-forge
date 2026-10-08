//! Secrets.

use super::*;

#[derive(Debug, Error)]
#[error("operating-system random number generation failed")]
pub struct SecretGenerationError;

pub trait SecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct OsSecretGenerator;

impl SecretGenerator for OsSecretGenerator {
    fn generate(&self, bytes: usize) -> Result<String, SecretGenerationError> {
        if bytes == 0 {
            return Err(SecretGenerationError);
        }
        let mut random = vec![0_u8; bytes];
        SystemRandom::new()
            .fill(&mut random)
            .map_err(|_| SecretGenerationError)?;
        Ok(hex::encode(random))
    }
}
