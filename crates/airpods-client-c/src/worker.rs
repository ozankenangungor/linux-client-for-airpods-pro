//! Safe worker control. SDK values and the runtime live only on the worker.

use crate::error::Error;
use crate::model::{Hello, HrSample, Status};
use airpods_client::{AirPodsClient, HeartRateSubscription};
use std::path::PathBuf;
use std::sync::{Mutex, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tokio::sync::mpsc as commands;

pub(crate) enum Connect {
    Default,
    Explicit(PathBuf),
}

pub(crate) enum Operation {
    Ping,
    Hello,
    Status,
    Subscribe,
    Next(Wait),
    Unsubscribe,
    Close,
    Shutdown,
}

pub(crate) enum Wait {
    Forever,
    Deadline(Instant),
}

impl Wait {
    pub fn milliseconds(timeout: u32) -> Self {
        if timeout == u32::MAX {
            Self::Forever
        } else {
            Self::Deadline(Instant::now() + Duration::from_millis(u64::from(timeout)))
        }
    }
}

#[derive(Debug)]
pub(crate) enum Reply {
    Unit,
    Hello(Hello),
    Status(Status),
    Sample(HrSample),
    Timeout,
    End,
}

struct Command {
    operation: Operation,
    reply: mpsc::SyncSender<Result<Reply, Error>>,
}

struct Control {
    sender: Option<commands::Sender<Command>>,
    join: Option<JoinHandle<()>>,
    closed: bool,
}

pub(crate) struct Client {
    control: Mutex<Control>,
}

impl Client {
    pub fn connect(target: Connect) -> Result<Self, Error> {
        let (sender, receiver) = commands::channel::<Command>(1);
        let (startup_tx, startup_rx) = mpsc::sync_channel(1);
        let join = thread::Builder::new()
            .name("airpods-client-c".to_owned())
            .spawn(move || run(target, receiver, startup_tx))
            .map_err(|_| Error::internal())?;
        let mut control = Control {
            sender: Some(sender),
            join: Some(join),
            closed: false,
        };
        match startup_rx.recv().unwrap_or_else(|_| Err(Error::internal())) {
            Ok(()) => Ok(Self {
                control: Mutex::new(control),
            }),
            Err(primary) => {
                // Startup failure is authoritative even if the thread also failed.
                let _ = control.stop(false);
                Err(primary)
            }
        }
    }

    pub fn request(&self, operation: Operation) -> Result<Reply, Error> {
        self.control
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .request(operation)
    }

    pub fn close(&self) -> Result<(), Error> {
        self.control
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .stop(true)
    }

    #[cfg(test)]
    pub fn joined(&self) -> bool {
        let control = self.control.lock().unwrap_or_else(|e| e.into_inner());
        control.closed && control.sender.is_none() && control.join.is_none()
    }
}

impl Drop for Control {
    fn drop(&mut self) {
        let _ = self.stop(false);
    }
}

impl Control {
    fn request(&self, operation: Operation) -> Result<Reply, Error> {
        if self.closed {
            return Err(Error::invalid_state("client is closed"));
        }
        let (reply, receiver) = mpsc::sync_channel(1);
        self.sender
            .as_ref()
            .ok_or_else(Error::internal)?
            // One caller and one outstanding command: the previous response
            // proves its command was consumed, so capacity is always available.
            .try_send(Command { operation, reply })
            .map_err(|_| Error::internal())?;
        receiver.recv().map_err(|_| Error::internal())?
    }

    fn stop(&mut self, confirmed: bool) -> Result<(), Error> {
        if self.closed {
            return Ok(());
        }
        let primary = if confirmed {
            self.request(Operation::Close).map(|_| ())
        } else {
            // No daemon response is awaited for destruction. Caller serialization
            // ensures the worker is idle, with no other command in flight.
            if let Some(sender) = &self.sender {
                let (reply, _) = mpsc::sync_channel(1);
                let _ = sender.try_send(Command {
                    operation: Operation::Shutdown,
                    reply,
                });
            }
            Ok(())
        };
        self.closed = true;
        self.sender.take();
        let joined = self.join.take().map_or(Ok(()), |join| match join.join() {
            Ok(()) => Ok(()),
            Err(payload) => {
                // Avoid a second panic from a custom payload's destructor and
                // preserve any primary SDK failure already returned above.
                std::mem::forget(payload);
                Err(Error::internal())
            }
        });
        primary.and(joined)
    }
}

struct Resources {
    subscription: Option<HeartRateSubscription>,
    client: AirPodsClient,
}

fn run(
    target: Connect,
    mut receiver: commands::Receiver<Command>,
    startup: mpsc::SyncSender<Result<(), Error>>,
) {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_io()
        .enable_time()
        .build()
    {
        Ok(runtime) => runtime,
        Err(_) => {
            let _ = startup.send(Err(Error::internal()));
            return;
        }
    };
    let resources = runtime.block_on(async move {
        let default = matches!(target, Connect::Default);
        let connected = match target {
            Connect::Default => AirPodsClient::connect().await,
            Connect::Explicit(path) => AirPodsClient::connect_to(path).await,
        };
        let client = match connected {
            Ok(client) => client,
            Err(error) => {
                let _ = startup.send(Err(Error::sdk(error, default)));
                return None;
            }
        };
        let mut resources = Resources {
            client,
            subscription: None,
        };
        if startup.send(Ok(())).is_err() {
            return Some(resources);
        }
        while let Some(command) = receiver.recv().await {
            if matches!(command.operation, Operation::Shutdown) {
                break;
            }
            let close = matches!(command.operation, Operation::Close);
            let result = resources.execute(command.operation).await;
            let _ = command.reply.send(result);
            if close {
                break;
            }
        }
        Some(resources)
    });
    // Outside block_on there is no current runtime context. The frozen SDK's
    // subscription Drop immediately closes the connection here, rather than
    // scheduling or awaiting an unsubscribe. Runtime Drop then cancels its reader.
    drop(resources);
    drop(runtime);
}

impl Resources {
    async fn execute(&mut self, operation: Operation) -> Result<Reply, Error> {
        match operation {
            Operation::Ping => self.client.ping().await.map(|()| Reply::Unit).map_err(sdk),
            Operation::Hello => self
                .client
                .hello()
                .await
                .map(|v| Reply::Hello(v.into()))
                .map_err(sdk),
            Operation::Status => self
                .client
                .status()
                .await
                .map(|v| Reply::Status(v.into()))
                .map_err(sdk),
            Operation::Subscribe => {
                // Delegate double-subscribe to the SDK to preserve its category.
                let subscription = self.client.subscribe_heart_rate().await.map_err(sdk)?;
                self.subscription = Some(subscription);
                Ok(Reply::Unit)
            }
            Operation::Unsubscribe => {
                let subscription = self
                    .subscription
                    .take()
                    .ok_or_else(|| Error::invalid_state("no active heart-rate subscription"))?;
                subscription
                    .unsubscribe()
                    .await
                    .map(|()| Reply::Unit)
                    .map_err(sdk)
            }
            Operation::Close => {
                if let Some(subscription) = self.subscription.take() {
                    subscription.unsubscribe().await.map_err(sdk)?;
                }
                Ok(Reply::Unit)
            }
            Operation::Next(wait) => {
                let subscription = self
                    .subscription
                    .as_mut()
                    .ok_or_else(|| Error::invalid_state("no active heart-rate subscription"))?;
                let next = match wait {
                    Wait::Forever => subscription.next().await,
                    Wait::Deadline(deadline) => tokio::select! {
                        biased;
                        next = subscription.next() => next,
                        () = tokio::time::sleep_until(deadline.into()) => return Ok(Reply::Timeout),
                    },
                };
                next.map(|v| v.map_or(Reply::End, |v| Reply::Sample(v.into())))
                    .map_err(sdk)
            }
            Operation::Shutdown => Err(Error::internal()),
        }
    }
}

fn sdk(error: airpods_client::Error) -> Error {
    Error::sdk(error, false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    #[test]
    fn best_effort_shutdown_joins_before_return() {
        let (sender, mut receiver) = commands::channel::<Command>(1);
        let exited = Arc::new(AtomicBool::new(false));
        let witness = Arc::clone(&exited);
        let join = thread::spawn(move || {
            assert!(matches!(
                receiver.blocking_recv().unwrap().operation,
                Operation::Shutdown
            ));
            witness.store(true, Ordering::SeqCst);
        });
        let mut control = Control {
            sender: Some(sender),
            join: Some(join),
            closed: false,
        };
        control.stop(false).unwrap();
        assert!(exited.load(Ordering::SeqCst));
        assert!(control.join.is_none());
        assert!(control.sender.is_none());
        assert!(control.closed);
    }

    #[test]
    fn worker_panic_and_channel_failure_are_internal_and_joined() {
        let (sender, receiver) = commands::channel::<Command>(1);
        let join = thread::spawn(move || {
            drop(receiver);
            panic!("test worker failure");
        });
        let mut control = Control {
            sender: Some(sender),
            join: Some(join),
            closed: false,
        };
        assert_eq!(control.request(Operation::Ping).err().unwrap().kind, 255);
        assert_eq!(control.stop(true).unwrap_err().kind, 255);
        assert!(control.join.is_none());
        assert!(control.sender.is_none());
        control.stop(true).unwrap();
    }

    #[test]
    fn cleanup_failure_does_not_replace_primary_daemon_error() {
        let (sender, mut receiver) = commands::channel::<Command>(1);
        let join = thread::spawn(move || {
            let command = receiver.blocking_recv().unwrap();
            assert!(matches!(command.operation, Operation::Close));
            command
                .reply
                .send(Err(Error::sdk(
                    airpods_client::Error::DaemonError {
                        code: "primary".into(),
                        message: "failed".into(),
                    },
                    false,
                )))
                .unwrap();
            panic!("test secondary worker failure");
        });
        let mut control = Control {
            sender: Some(sender),
            join: Some(join),
            closed: false,
        };
        let error = control.stop(true).unwrap_err();
        assert_eq!(error.kind, 17);
        assert_eq!(error.daemon_code.as_deref(), Some("primary"));
        assert!(control.join.is_none());
    }
}
