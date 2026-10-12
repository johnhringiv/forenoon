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
use forenoon_web::{Error, Options, app, auth, db};

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
        /// Send the session cookie only over https.
        #[arg(long, env = "FORENOON_WEB_SECURE_COOKIES")]
        secure_cookies: bool,
    },
    /// Create an account. The id must be the reader's id in the pipeline's config.
    AddUser {
        id: String,
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
        Command::Serve { listen, secure_cookies } => {
            if db::user_count(&pool).await? == 0 {
                eprintln!("No accounts yet, so nobody can sign in. Create one with: forenoon-web add-user <id>");
            }
            let router = app(pool, Options { secure_cookies }).await?;
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
        Command::AddUser { id, name, role, timezone, kindle } => {
            if !auth::valid_id(&id) {
                return Err("the id must be 1 to 64 letters, digits, '_', '.' or '-'".into());
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
            db::add_user(&pool, &id, name.as_deref().unwrap_or(&id), &hash, &role, &timezone).await?;
            db::set_kindle_addresses(&pool, &id, &kindle.join("\n")).await?;
            println!("Created {role} account {id} ({timezone}).");
            if kindle.is_empty() {
                println!("It has no Kindle address, so nothing will be sent to it; set one on the account page.");
            }
            if !db::pipeline_ready(&pool).await? || db::totals(&pool, &id).await?.articles == 0 {
                println!("Nothing has been delivered to {id} yet.");
            }
        }
        Command::SetPassword { id } => {
            let hash = auth::hash_password(read_password()?).await?;
            if !db::set_password(&pool, &id, &hash).await? {
                return Err(format!("no account {id}").into());
            }
            println!("Password changed for {id}; it is signed out everywhere.");
        }
        Command::Users => {
            for user in db::list_users(&pool).await? {
                let kindle = user.kindle_addresses.lines().collect::<Vec<_>>().join(", ");
                println!(
                    "{:<16} {:<7} {:<24} {}  {}  [{}]",
                    user.id, user.role, user.timezone, user.created_at, user.name, kindle
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
