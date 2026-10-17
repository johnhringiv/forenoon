//! `forenoon-web`: serve the pages, and manage the accounts that can sign in to them.

use std::{
    io::{self, BufRead, IsTerminal, Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    process::ExitCode,
    str::FromStr,
    time::Duration,
};

use chrono_tz::Tz;
use clap::{Parser, Subcommand};
use forenoon_web::{
    Error, Options, app, auth, db,
    mail::{Mailer, Smtp},
    proxy::Proxy,
};

#[derive(Parser)]
#[command(name = "forenoon-web", about = "Forenoon's web pages", version)]
struct Cli {
    /// The database shared with the pipeline.
    #[arg(long, env = "FORENOON_DB", default_value = "/data/forenoon.sqlite3", global = true)]
    db: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Serve the pages.
    Serve {
        #[arg(long, env = "FORENOON_WEB_LISTEN", default_value = "0.0.0.0:8081")]
        listen: SocketAddr,
        /// The pages are reached over https (through a tunnel or a reverse proxy): send the
        /// session cookie only that way, and tell browsers to use nothing else.
        #[arg(long, env = "FORENOON_WEB_SECURE_COOKIES")]
        secure_cookies: bool,
        /// The header a proxy in front puts the client's address in: CF-Connecting-IP for a
        /// Cloudflare tunnel, X-Forwarded-For or X-Real-IP for most reverse proxies.
        #[arg(long, env = "FORENOON_WEB_CLIENT_IP_HEADER", default_value = "")]
        client_ip_header: String,
        /// Addresses and networks that header is believed from, comma-separated. The default
        /// fits a tunnel sharing this container's network.
        #[arg(long, env = "FORENOON_WEB_TRUSTED_PROXIES", default_value = "127.0.0.1,::1")]
        trusted_proxies: String,
        /// Where readers reach the pages, for the links in emails: http://nas.lan:8081
        #[arg(long, env = "FORENOON_WEB_URL")]
        url: Option<String>,
        /// Mail server for confirmation and password-reset emails. Without one they are
        /// written to the log.
        #[arg(long, env = "FORENOON_SMTP_HOST")]
        smtp_host: Option<String>,
        #[arg(long, env = "FORENOON_SMTP_PORT", default_value_t = 587)]
        smtp_port: u16,
        /// TLS from the start (port 465) instead of STARTTLS.
        #[arg(long, env = "FORENOON_SMTP_IMPLICIT_TLS")]
        smtp_implicit_tls: bool,
        #[arg(long, env = "FORENOON_SMTP_USERNAME", default_value = "")]
        smtp_username: String,
        /// The From address.
        #[arg(long, env = "FORENOON_SMTP_SENDER", default_value = "")]
        smtp_sender: String,
        /// Where subscriber session cookies are kept: a folder only this program and the browser
        /// container can read, never one inside the data folder. Without it, sessions cannot be saved.
        #[arg(long, env = "FORENOON_LOGINS_DIR")]
        logins_dir: Option<PathBuf>,
        /// The hostname the project's landing page (built into this program) is served at,
        /// for the public site: forenoon.dev. Route it here as well. Empty for an ordinary install.
        #[arg(long, env = "FORENOON_WEB_LANDING", default_value = "")]
        landing: String,
    },
    /// Create an account. The id names the reader in the pipeline's records and never changes.
    AddUser {
        id: String,
        /// What the reader signs in with.
        #[arg(long)]
        email: String,
        /// Display name; defaults to the id.
        #[arg(long)]
        name: Option<String>,
        /// admin or reader; defaults to admin for the first account and reader after it.
        #[arg(long)]
        role: Option<String>,
        /// Zone the reader's dates are shown in; defaults to $TZ, then UTC.
        #[arg(long)]
        timezone: Option<String>,
        /// A Send to Kindle address the reader's books go to; may be given more than once.
        #[arg(long)]
        kindle: Vec<String>,
        /// Start with no sources; by default the account follows every source shared with
        /// the household.
        #[arg(long)]
        follow_nothing: bool,
    },
    /// Change an account's password, signing it out everywhere.
    SetPassword { id: String },
    /// List the accounts.
    Users,
    /// Exit 0 when a server on this machine answers its health check.
    Health {
        #[arg(long, default_value_t = 8081)]
        port: u16,
    },
}

/// From the terminal, asked twice without echo; otherwise the first line of standard input.
fn read_password() -> Result<String, Error> {
    let password = if io::stdin().is_terminal() {
        let first = rpassword::prompt_password("Password: ")?;
        if rpassword::prompt_password("Password again: ")? != first {
            return Err("the two passwords are not the same".into());
        }
        first
    } else {
        let mut line = String::new();
        io::stdin().lock().read_line(&mut line)?;
        line.trim_end_matches(['\r', '\n']).to_owned()
    };
    match auth::password_problem(&password) {
        Some(problem) => Err(problem.into()),
        None => Ok(password),
    }
}

fn check_health(port: u16) -> Result<(), Error> {
    let mut stream = TcpStream::connect_timeout(&SocketAddr::from(([127, 0, 0, 1], port)), Duration::from_secs(3))?;
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.write_all(b"GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n")?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply)?;
    if reply.starts_with("HTTP/1.0 200") || reply.starts_with("HTTP/1.1 200") {
        Ok(())
    } else {
        Err(format!("unexpected reply: {}", reply.lines().next().unwrap_or("")).into())
    }
}

async fn run(cli: Cli) -> Result<(), Error> {
    if let Command::Health { port } = cli.command {
        return check_health(port);
    }
    let pool = db::open(&cli.db).await?;
    match cli.command {
        Command::Serve {
            listen,
            secure_cookies,
            client_ip_header,
            trusted_proxies,
            url,
            smtp_host,
            smtp_port,
            smtp_implicit_tls,
            smtp_username,
            smtp_sender,
            logins_dir,
            landing,
        } => {
            if db::user_count(&pool).await? == 0 {
                eprintln!("No accounts yet. Create one with: forenoon-web add-user <id> --email <address>");
            }
            let mailer = match smtp_host.filter(|host| !host.is_empty()) {
                Some(host) => Mailer::smtp(Smtp {
                    host,
                    port: smtp_port,
                    starttls: !smtp_implicit_tls,
                    username: smtp_username,
                    // Only ever from the environment, so it cannot show up in a process list.
                    password: std::env::var("FORENOON_SMTP_PASSWORD").unwrap_or_default(),
                    sender: smtp_sender.clone(),
                })?,
                None => {
                    eprintln!("No mail server set (FORENOON_SMTP_HOST): account emails go to this log.");
                    Mailer::Log
                }
            };
            let base_url = url.filter(|url| !url.is_empty()).unwrap_or_else(|| {
                eprintln!("FORENOON_WEB_URL is not set: links in emails will point at http://{listen}");
                format!("http://{listen}")
            });
            let data_dir = cli.db.parent().map(PathBuf::from).unwrap_or_default();
            let proxy = Proxy::new(&client_ip_header, &trusted_proxies)?;
            // Only ever from the environment, like the mail password. Without it, or with one
            // too short to sign anything with, the rating links in books are not answered; the
            // rest of the pages still serve, since the tunnel lives in this container too.
            let feedback_secret = match std::env::var("FORENOON_FEEDBACK_SECRET").unwrap_or_default() {
                secret if secret.is_empty() => {
                    eprintln!("FORENOON_FEEDBACK_SECRET is not set: rating links in books will not work.");
                    None
                }
                secret if secret.len() < 32 => {
                    eprintln!(
                        "FORENOON_FEEDBACK_SECRET is shorter than 32 characters: rating links in books will not work. Generate one with: openssl rand -hex 32"
                    );
                    None
                }
                secret => Some(secret.into_bytes()),
            };
            let sender = Some(smtp_sender).filter(|sender| !sender.is_empty());
            let landing =
                forenoon_web::landing::host(&landing).map_err(|problem| format!("FORENOON_WEB_LANDING: {problem}"))?;
            let options = Options {
                secure_cookies,
                mailer,
                base_url,
                data_dir,
                logins_dir,
                proxy,
                feedback_secret,
                sender,
                landing,
            };
            let router = app(pool, options).await?;
            let listener = tokio::net::TcpListener::bind(listen).await?;
            eprintln!("listening on {listen}");
            axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>())
                .with_graceful_shutdown(async {
                    let mut stop = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).ok();
                    tokio::select! {
                        _ = tokio::signal::ctrl_c() => {}
                        _ = async { match stop.as_mut() { Some(stop) => { stop.recv().await; } None => std::future::pending().await } } => {}
                    }
                })
                .await?;
        }
        Command::AddUser { id, email, name, role, timezone, kindle, follow_nothing } => {
            if !auth::valid_id(&id) {
                return Err(
                    "the id must be 1 to 64 letters, digits, '_', '.' or '-', with a letter or digit in it".into()
                );
            }
            let Some(email) = auth::normal_email(&email) else {
                return Err(format!("{email} is not an email address").into());
            };
            if db::email_taken(&pool, &email, &id).await? {
                return Err(format!("another account already uses {email}").into());
            }
            if auth::find_user(&pool, &id).await?.is_some() {
                return Err(format!("account {id} already exists; use set-password to change its password").into());
            }
            let first = db::user_count(&pool).await? == 0;
            let role = role.unwrap_or_else(|| if first { "admin" } else { "reader" }.to_owned());
            if !auth::ROLES.contains(&role.as_str()) {
                return Err("the role must be admin or reader".into());
            }
            let timezone = timezone.or_else(|| std::env::var("TZ").ok().filter(|zone| Tz::from_str(zone).is_ok()));
            let timezone = timezone.unwrap_or_else(|| "UTC".to_owned());
            if Tz::from_str(&timezone).is_err() {
                return Err(format!("{timezone} is not a time zone name; use one such as America/New_York").into());
            }
            let kindle = auth::kindle_addresses(&kindle.join("\n"))?;
            let hash = auth::hash_password(read_password()?).await?;
            db::add_user(&pool, &id, &email, name.as_deref().unwrap_or(&id), &hash, &role, &timezone).await?;
            db::set_kindle(&pool, &id, &kindle.join("\n"), true, true).await?;
            if !follow_nothing {
                db::follow_shared(&pool, &id).await?;
            }
            println!("Created {role} account {id} ({timezone}). Sign in with {email}.");
            if kindle.is_empty() {
                println!(
                    "It has no Kindle address, so its issues are read on the web pages; set one on the account page to have them sent."
                );
            }
            if !db::pipeline_ready(&pool).await? || db::totals(&pool, &id).await?.articles == 0 {
                println!("Nothing has been delivered to {id} yet.");
            }
        }
        Command::SetPassword { id } => {
            if auth::find_user(&pool, &id).await?.is_some_and(|user| user.is_invited()) {
                return Err(format!(
                    "{id} is an invitation not yet accepted; send its link again or take it back on the Accounts page"
                )
                .into());
            }
            let hash = auth::hash_password(read_password()?).await?;
            if !db::set_password(&pool, &id, &hash).await? {
                return Err(format!("no account {id}").into());
            }
            println!("Password changed for {id}; it is signed out everywhere.");
        }
        Command::Users => {
            for user in db::list_users(&pool).await? {
                let kindle = db::kindle_line(&user);
                let email = match (&user.email, &user.email_verified_at, &user.invited_at) {
                    (Some(email), _, Some(_)) => format!("{email} (invited, not yet joined)"),
                    (Some(email), Some(_), None) => format!("{email} (confirmed)"),
                    (Some(email), None, None) => format!("{email} (not confirmed)"),
                    (None, _, _) => "no email: cannot sign in".to_owned(),
                };
                println!(
                    "{:<12} {:<7} {}  {}  kindle [{}]  {}",
                    user.id, user.role, email, user.name, kindle, user.timezone
                );
            }
        }
        Command::Health { .. } => unreachable!("handled above"),
    }
    Ok(())
}

#[tokio::main]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("forenoon-web: {error}");
            ExitCode::FAILURE
        }
    }
}
