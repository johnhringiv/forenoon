//! Sending the account emails: confirm an address, reset a password.

use std::{
    sync::{Arc, Mutex},
    time::Duration,
};

use lettre::{
    AsyncSmtpTransport, AsyncTransport, Tokio1Executor,
    message::{Mailbox, header::ContentType},
    transport::smtp::authentication::Credentials,
};

use crate::Error;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Message {
    pub to: String,
    pub subject: String,
    pub body: String,
}

#[derive(Clone)]
pub enum Mailer {
    Smtp(Arc<SmtpSender>),
    /// No mail server is configured: messages are written to the server log instead, where
    /// whoever runs the install can read the link.
    Log,
    /// Kept in memory, for tests.
    Memory(Arc<Mutex<Vec<Message>>>),
}

pub struct SmtpSender {
    transport: AsyncSmtpTransport<Tokio1Executor>,
    sender: Mailbox,
}

pub struct Smtp {
    pub host: String,
    pub port: u16,
    /// Upgrade a plain connection with STARTTLS (port 587); otherwise TLS from the start (465).
    pub starttls: bool,
    pub username: String,
    pub password: String,
    pub sender: String,
}

impl Mailer {
    pub fn smtp(smtp: Smtp) -> Result<Self, Error> {
        let builder = if smtp.starttls {
            AsyncSmtpTransport::<Tokio1Executor>::starttls_relay(&smtp.host)?
        } else {
            AsyncSmtpTransport::<Tokio1Executor>::relay(&smtp.host)?
        };
        let mut builder = builder.port(smtp.port).timeout(Some(Duration::from_secs(20)));
        if !smtp.username.is_empty() {
            builder = builder.credentials(Credentials::new(smtp.username, smtp.password));
        }
        let sender = smtp.sender.parse().map_err(|e| format!("the sender address is not valid: {e}"))?;
        Ok(Mailer::Smtp(Arc::new(SmtpSender { transport: builder.build(), sender })))
    }

    pub async fn send(&self, message: Message) -> Result<(), Error> {
        match self {
            Mailer::Smtp(smtp) => {
                let email = lettre::Message::builder()
                    .from(smtp.sender.clone())
                    .to(message.to.parse()?)
                    .subject(message.subject)
                    .header(ContentType::TEXT_PLAIN)
                    .body(message.body)?;
                smtp.transport.send(email).await?;
            }
            Mailer::Log => {
                eprintln!("mail (no mail server configured) to {}: {}\n{}", message.to, message.subject, message.body)
            }
            Mailer::Memory(sent) => sent.lock().unwrap_or_else(|poisoned| poisoned.into_inner()).push(message),
        }
        Ok(())
    }
}
