//! One worker owns the existing resilient client. Rendering only drains a queue.

use crate::bootstrap::{self, Failure};
use crate::demo::DemoSource;
use crate::model::{AppEvent, Problem, TimedEvent};
use airpods_client::Error as ClientError;
use airpods_client_resilient::{
    Error, ReconnectPolicy, ResilientHeartRateEvent, ResilientHeartRateStream,
};
use eframe::egui::Context;
use std::path::PathBuf;
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, watch};

const EVENT_CAPACITY: usize = 256;
const CLOSE_TIMEOUT: Duration = Duration::from_millis(750);

pub enum DataSource {
    Demo(DemoSource),
    Real(RealSource),
    RealFailed,
}

impl DataSource {
    pub fn start(
        demo: bool,
        socket: Option<PathBuf>,
        ctx: Context,
        started: Instant,
    ) -> std::io::Result<Self> {
        Self::choose(demo, || RealSource::start(socket, ctx, started))
    }

    fn choose(
        demo: bool,
        real: impl FnOnce() -> std::io::Result<RealSource>,
    ) -> std::io::Result<Self> {
        if demo {
            Ok(Self::Demo(DemoSource::default()))
        } else {
            real().map(Self::Real)
        }
    }

    pub fn poll(&mut self, elapsed: Duration) -> Vec<TimedEvent> {
        match self {
            Self::Demo(demo) => demo.poll(elapsed),
            Self::Real(real) => real.poll(),
            Self::RealFailed => Vec::new(),
        }
    }

    pub fn time(&self, elapsed: Duration) -> Duration {
        match self {
            Self::Demo(_) => DemoSource::time(elapsed),
            Self::Real(_) => elapsed,
            Self::RealFailed => elapsed,
        }
    }

    pub fn is_demo(&self) -> bool {
        matches!(self, Self::Demo(_))
    }

    pub fn retry(&self) -> bool {
        match self {
            Self::Demo(_) => false,
            Self::Real(real) => real.retry.try_send(()).is_ok(),
            Self::RealFailed => false,
        }
    }

    pub fn shutdown(&mut self) {
        if let Self::Real(real) = self {
            real.shutdown();
        }
    }
}

pub struct RealSource {
    events: mpsc::Receiver<TimedEvent>,
    stop: watch::Sender<bool>,
    retry: mpsc::Sender<()>,
    thread: Option<JoinHandle<()>>,
    disconnected_reported: bool,
    started: Instant,
}

impl RealSource {
    pub fn start(socket: Option<PathBuf>, ctx: Context, started: Instant) -> std::io::Result<Self> {
        let (sender, events) = mpsc::channel(EVENT_CAPACITY);
        let (stop, stopped) = watch::channel(false);
        let (retry, retries) = mpsc::channel(1);
        let thread = thread::Builder::new()
            .name("airpods-desktop-ipc".into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(_) => {
                        let _ = sender.try_send(TimedEvent {
                            at: started.elapsed(),
                            event: AppEvent::Failed(Problem::Worker),
                        });
                        ctx.request_repaint();
                        return;
                    }
                };
                runtime.block_on(drive(
                    socket,
                    started,
                    sender,
                    stopped,
                    retries,
                    ctx,
                    ReconnectPolicy::default(),
                ));
                // All socket operations are async. Runtime teardown cancels the base
                // client's reader and any best-effort subscription cleanup tasks.
                runtime.shutdown_timeout(Duration::from_millis(50));
            })?;
        Ok(Self {
            events,
            stop,
            retry,
            thread: Some(thread),
            disconnected_reported: false,
            started,
        })
    }

    fn poll(&mut self) -> Vec<TimedEvent> {
        let mut result = Vec::new();
        for _ in 0..EVENT_CAPACITY {
            match self.events.try_recv() {
                Ok(event) => result.push(event),
                Err(mpsc::error::TryRecvError::Empty) => break,
                Err(mpsc::error::TryRecvError::Disconnected) => {
                    if !self.disconnected_reported && !*self.stop.borrow() {
                        self.disconnected_reported = true;
                        result.push(TimedEvent {
                            at: self.started.elapsed(),
                            event: AppEvent::Failed(Problem::Worker),
                        });
                    }
                    break;
                }
            }
        }
        result
    }

    fn request_stop(&self) {
        let _ = self.stop.send(true);
    }

    fn shutdown(&mut self) {
        self.request_stop();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for RealSource {
    fn drop(&mut self) {
        self.shutdown();
    }
}

async fn emit(
    sender: &mpsc::Sender<TimedEvent>,
    stop: &mut watch::Receiver<bool>,
    ctx: &Context,
    started: Instant,
    event: AppEvent,
) -> bool {
    if *stop.borrow() {
        return false;
    }
    let result = tokio::select! {
        biased;
        _ = stop.changed() => false,
        result = sender.send(TimedEvent { at: started.elapsed(), event }) => result.is_ok(),
    };
    if result {
        ctx.request_repaint();
    }
    result
}

async fn drive(
    socket: Option<PathBuf>,
    started: Instant,
    sender: mpsc::Sender<TimedEvent>,
    mut stop: watch::Receiver<bool>,
    mut retries: mpsc::Receiver<()>,
    ctx: Context,
    policy: ReconnectPolicy,
) {
    loop {
        if socket.is_none() {
            let progress_stop = stop.clone();
            let report = |stage| {
                let sender = sender.clone();
                let mut stopped = progress_stop.clone();
                let ctx = ctx.clone();
                async move {
                    emit(
                        &sender,
                        &mut stopped,
                        &ctx,
                        started,
                        AppEvent::Bootstrap(stage),
                    )
                    .await
                }
            };
            match bootstrap::prepare_default(&mut stop, report).await {
                Ok(()) => {}
                Err(Failure::Cancelled) => return,
                Err(failure) => {
                    if !emit(
                        &sender,
                        &mut stop,
                        &ctx,
                        started,
                        AppEvent::Failed(Problem::Bootstrap(failure)),
                    )
                    .await
                        || !wait_retry(&mut stop, &mut retries).await
                    {
                        return;
                    }
                    continue;
                }
            }
        }
        if !emit(&sender, &mut stop, &ctx, started, AppEvent::Connecting).await {
            return;
        }
        let mut stream = match &socket {
            Some(path) => ResilientHeartRateStream::explicit_socket(path, policy.clone()),
            None => ResilientHeartRateStream::default_socket(policy.clone()),
        };
        let shutdown = loop {
            // This future is not cancelled by UI ticks or repainting. The only
            // cancellation is final shutdown, including a stalled subscribe.
            let result = tokio::select! {
                biased;
                _ = stop.changed() => break true,
                result = stream.next() => result,
            };
            let (event, terminal) = match result {
                Ok(Some(event)) => (application_event(event), false),
                Ok(None) => (AppEvent::Disconnected, true),
                Err(error) => (AppEvent::Failed(problem(error)), true),
            };
            if !emit(&sender, &mut stop, &ctx, started, event).await {
                break true;
            }
            if terminal {
                break false;
            }
        };
        // Prefer confirmed unsubscribe. A silent or stalled daemon cannot hold
        // window close forever; the SDK's cancellation/drop cleanup then applies.
        let _ = tokio::time::timeout(CLOSE_TIMEOUT, stream.close()).await;
        if shutdown {
            return;
        }
        // Exhaustion and protocol errors remain terminal. Only an explicit user
        // retry creates a new finite episode; there is no second retry algorithm.
        if !wait_retry(&mut stop, &mut retries).await {
            return;
        }
    }
}

async fn wait_retry(stop: &mut watch::Receiver<bool>, retries: &mut mpsc::Receiver<()>) -> bool {
    if *stop.borrow() {
        return false;
    }
    tokio::select! {
        biased;
        _ = stop.changed() => false,
        command = retries.recv() => command.is_some(),
    }
}

fn application_event(event: ResilientHeartRateEvent) -> AppEvent {
    match event {
        ResilientHeartRateEvent::Sample(sample) => AppEvent::Sample(sample),
        ResilientHeartRateEvent::Reconnecting { attempt, delay } => {
            AppEvent::Reconnecting { attempt, delay }
        }
        ResilientHeartRateEvent::Reconnected { attempts } => AppEvent::Reconnected { attempts },
    }
}

fn problem(error: Error) -> Problem {
    match error {
        Error::RetryExhausted { .. } => Problem::Unavailable,
        Error::Client(error) => match error {
            ClientError::XdgRuntimeDirMissing => Problem::Configuration,
            ClientError::Connect {
                kind: std::io::ErrorKind::PermissionDenied,
                ..
            }
            | ClientError::Io {
                kind: std::io::ErrorKind::PermissionDenied,
                ..
            } => Problem::Permission,
            ClientError::Connect { .. }
            | ClientError::ConnectionClosed
            | ClientError::Io { .. } => Problem::Unavailable,
            ClientError::EventLagged { .. } => Problem::Lagged,
            ClientError::DaemonError { code, .. }
                if matches!(
                    code.as_str(),
                    "service_unavailable" | "service_failed" | "session_start_failed"
                ) =>
            {
                Problem::SensorUnavailable
            }
            ClientError::DaemonError { .. } | ClientError::SubscriptionActive => Problem::Rejected,
            _ => Problem::Protocol,
        },
    }
}

#[cfg(test)]
mod tests;
