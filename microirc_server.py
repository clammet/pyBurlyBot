#!/usr/bin/env python3
"""A deliberately tiny IRC server for the pyBurlyBot smoke-test harness."""

from __future__ import annotations

import argparse
import socketserver
import sys
from dataclasses import dataclass, field
from threading import RLock

from microirc import IRCMessage, MicroIRCError, parse_irc_line

SERVER_NAME = "microirc.local"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6667


@dataclass(eq=False)
class ClientState:
    handler: IRCRelayHandler
    nickname: str | None = None
    username: str | None = None
    registered: bool = False
    closed: bool = False
    channels: dict[str, str] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        nickname = self.nickname or "unknown"
        username = self.username or "unknown"
        return f"{nickname}!{username}@{SERVER_NAME}"


class IRCRelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded relay with only the IRC operations needed by bot and client."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        encoding: str = "utf-8",
        verbose: bool = False,
    ) -> None:
        self.encoding = encoding
        self.verbose = verbose
        self.clients: set[ClientState] = set()
        self.state_lock = RLock()
        super().__init__(address, IRCRelayHandler)

    def add_client(self, handler: IRCRelayHandler) -> ClientState:
        state = ClientState(handler)
        with self.state_lock:
            self.clients.add(state)
        return state

    def client_joined(self, nickname: str, channel: str) -> bool:
        """Report whether a registered client has joined a channel."""
        nickname_key = nickname.casefold()
        channel_key = channel.casefold()
        with self.state_lock:
            return any(
                client.registered
                and not client.closed
                and client.nickname is not None
                and client.nickname.casefold() == nickname_key
                and channel_key in client.channels
                for client in self.clients
            )

    def disconnect(self, state: ClientState, reason: str) -> None:
        with self.state_lock:
            if state.closed:
                return
            peers = self._shared_peers(state)
            state.closed = True
            state.channels.clear()
            self.clients.discard(state)
        if state.registered:
            self.broadcast(peers, f":{state.prefix} QUIT :{reason}")

    def handle_message(self, state: ClientState, message: IRCMessage) -> bool:
        if message.prefix is not None:
            state.handler.send_line("ERROR :Client-supplied prefixes are not allowed")
            return False

        handler = getattr(self, f"_irc_{message.command}", None)
        if handler is None:
            self.numeric(
                state,
                "421",
                message.command,
                trailing="Unknown command",
            )
            return True
        return bool(handler(state, message.params))

    def _irc_PASS(self, state: ClientState, params: tuple[str, ...]) -> bool:
        # Authentication is intentionally outside this server's scope.
        return True

    def _irc_NICK(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not params or not self._valid_nickname(params[0]):
            self.numeric(
                state,
                "432",
                params[0] if params else "*",
                trailing="Erroneous nickname",
            )
            return True
        nickname = params[0]
        with self.state_lock:
            collision = next(
                (
                    client
                    for client in self.clients
                    if client is not state
                    and client.nickname
                    and client.nickname.casefold() == nickname.casefold()
                ),
                None,
            )
            if collision is not None:
                self.numeric(
                    state, "433", nickname, trailing="Nickname is already in use"
                )
                return True
            old_prefix = state.prefix
            peers = self._shared_peers(state)
            was_registered = state.registered
            state.nickname = nickname
        if was_registered:
            self.broadcast(peers | {state}, f":{old_prefix} NICK :{nickname}")
        self._complete_registration(state)
        return True

    def _irc_USER(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if len(params) < 4:
            self.numeric(state, "461", "USER", trailing="Not enough parameters")
            return True
        if state.registered:
            self.numeric(state, "462", trailing="Already registered")
            return True
        state.username = params[0]
        self._complete_registration(state)
        return True

    def _irc_JOIN(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not self._require_registered(state) or not params:
            if not params:
                self.numeric(state, "461", "JOIN", trailing="Not enough parameters")
            return True
        for channel in params[0].split(","):
            if not channel or channel[0] not in "#&":
                self.numeric(state, "403", channel or "*", trailing="No such channel")
                continue
            key = channel.casefold()
            with self.state_lock:
                state.channels[key] = channel
                members = self._channel_members(key)
            self.broadcast(members, f":{state.prefix} JOIN :{channel}")
            names = " ".join(
                sorted(
                    (member.nickname or "unknown" for member in members),
                    key=str.casefold,
                )
            )
            self.numeric(state, "353", "=", channel, trailing=names)
            self.numeric(state, "366", channel, trailing="End of /NAMES list")
        return True

    def _irc_PART(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not self._require_registered(state) or not params:
            if not params:
                self.numeric(state, "461", "PART", trailing="Not enough parameters")
            return True
        reason = params[1] if len(params) > 1 else "Leaving"
        for requested in params[0].split(","):
            key = requested.casefold()
            with self.state_lock:
                channel = state.channels.get(key)
                if channel is None:
                    self.numeric(
                        state, "442", requested, trailing="You're not on that channel"
                    )
                    continue
                members = self._channel_members(key)
                del state.channels[key]
            self.broadcast(members, f":{state.prefix} PART {channel} :{reason}")
        return True

    def _irc_PRIVMSG(self, state: ClientState, params: tuple[str, ...]) -> bool:
        return self._relay_message(state, "PRIVMSG", params, report_errors=True)

    def _irc_NOTICE(self, state: ClientState, params: tuple[str, ...]) -> bool:
        return self._relay_message(state, "NOTICE", params, report_errors=False)

    def _irc_MODE(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not self._require_registered(state) or not params:
            if not params:
                self.numeric(state, "461", "MODE", trailing="Not enough parameters")
            return True
        target = params[0]
        if len(params) == 1 and target[:1] in "#&":
            self.numeric(state, "324", target, "+nt")
            return True
        if target[:1] in "#&":
            with self.state_lock:
                members = self._channel_members(target.casefold())
            line = " ".join((f":{state.prefix}", "MODE", *params))
            self.broadcast(members, line)
        return True

    def _irc_NAMES(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not self._require_registered(state):
            return True
        channels = params[0].split(",") if params else tuple(state.channels.values())
        for channel in channels:
            with self.state_lock:
                members = self._channel_members(channel.casefold())
            names = " ".join(
                sorted(
                    (member.nickname or "unknown" for member in members),
                    key=str.casefold,
                )
            )
            self.numeric(state, "353", "=", channel, trailing=names)
            self.numeric(state, "366", channel, trailing="End of /NAMES list")
        return True

    def _irc_PING(self, state: ClientState, params: tuple[str, ...]) -> bool:
        if not params:
            self.numeric(state, "409", trailing="No origin specified")
        else:
            state.handler.send_line(f":{SERVER_NAME} PONG {SERVER_NAME} :{params[-1]}")
        return True

    def _irc_PONG(self, state: ClientState, params: tuple[str, ...]) -> bool:
        return True

    def _irc_QUIT(self, state: ClientState, params: tuple[str, ...]) -> bool:
        self.disconnect(state, params[0] if params else "Client quit")
        return False

    def _relay_message(
        self,
        state: ClientState,
        command: str,
        params: tuple[str, ...],
        *,
        report_errors: bool,
    ) -> bool:
        if not self._require_registered(state):
            return True
        if not params:
            if report_errors:
                self.numeric(state, "411", trailing="No recipient given")
            return True
        if len(params) < 2 or not params[1]:
            if report_errors:
                self.numeric(state, "412", trailing="No text to send")
            return True
        target, text = params[0], params[1]
        with self.state_lock:
            if target[:1] in "#&":
                recipients = self._channel_members(target.casefold()) - {state}
                exists = any(
                    target.casefold() in client.channels for client in self.clients
                )
            else:
                recipient = next(
                    (
                        client
                        for client in self.clients
                        if client.nickname
                        and client.nickname.casefold() == target.casefold()
                    ),
                    None,
                )
                recipients = {recipient} if recipient is not None else set()
                exists = recipient is not None
        if not exists:
            if report_errors:
                code = "403" if target[:1] in "#&" else "401"
                self.numeric(state, code, target, trailing="No such channel/nick")
            return True
        self.broadcast(
            recipients,
            f":{state.prefix} {command} {target} :{text}",
        )
        return True

    def _complete_registration(self, state: ClientState) -> None:
        if state.registered or not state.nickname or not state.username:
            return
        state.registered = True
        self.numeric(
            state,
            "001",
            trailing=f"Welcome to the micro IRC relay, {state.nickname}",
        )
        self.numeric(
            state,
            "005",
            "CHANTYPES=#&",
            "PREFIX=(ohv)@%+",
            "CASEMAPPING=ascii",
            trailing="are supported by this server",
        )
        self.numeric(state, "376", trailing="End of /MOTD command")

    def _require_registered(self, state: ClientState) -> bool:
        if state.registered:
            return True
        self.numeric(state, "451", trailing="You have not registered")
        return False

    def _channel_members(self, channel_key: str) -> set[ClientState]:
        return {
            client
            for client in self.clients
            if client.registered
            and not client.closed
            and channel_key in client.channels
        }

    def _shared_peers(self, state: ClientState) -> set[ClientState]:
        peers: set[ClientState] = set()
        for channel_key in state.channels:
            peers.update(self._channel_members(channel_key))
        peers.discard(state)
        return peers

    @staticmethod
    def _valid_nickname(nickname: str) -> bool:
        return bool(nickname) and not any(
            character.isspace() or character in ",:*?!@.#&" for character in nickname
        )

    def numeric(
        self,
        state: ClientState,
        code: str,
        *params: str,
        trailing: str | None = None,
    ) -> None:
        nickname = state.nickname or "*"
        line = " ".join((f":{SERVER_NAME}", code, nickname, *params))
        if trailing is not None:
            line += f" :{trailing}"
        state.handler.send_line(line)

    @staticmethod
    def broadcast(recipients: set[ClientState], line: str) -> None:
        for recipient in tuple(recipients):
            recipient.handler.send_line(line)


class IRCRelayHandler(socketserver.StreamRequestHandler):
    server: IRCRelayServer

    def setup(self) -> None:
        super().setup()
        self.send_lock = RLock()
        self.state = self.server.add_client(self)

    def handle(self) -> None:
        while True:
            encoded = self.rfile.readline(513)
            if not encoded:
                return
            if len(encoded) > 512 or not encoded.endswith(b"\r\n"):
                self.send_line(
                    "ERROR :IRC lines must end with CRLF and fit in 512 bytes"
                )
                return
            try:
                line = encoded[:-2].decode(self.server.encoding)
                message = parse_irc_line(line)
            except (UnicodeError, MicroIRCError) as exc:
                self.send_line(f"ERROR :Malformed IRC message: {exc}")
                return
            if self.server.verbose:
                label = (
                    self.state.nickname
                    or f"{self.client_address[0]}:{self.client_address[1]}"
                )
                print(f"<{label}> {message.raw}")
            if not self.server.handle_message(self.state, message):
                return

    def finish(self) -> None:
        self.server.disconnect(self.state, "Connection closed")
        super().finish()

    def send_line(self, line: str) -> None:
        if self.state.closed:
            return
        if any(character in line for character in "\r\n\0"):
            raise MicroIRCError("Server attempted to send an invalid IRC line")
        encoded = line.encode(self.server.encoding)
        if len(encoded) > 510:
            # Truncate rather than raise: an overlong relayed line must not kill
            # the sender's handler thread.  Drop any trailing partial character.
            line = encoded[:510].decode(self.server.encoding, "ignore")
            encoded = line.encode(self.server.encoding)
        if self.server.verbose:
            label = (
                self.state.nickname
                or f"{self.client_address[0]}:{self.client_address[1]}"
            )
            print(f">{label}> {line}")
        try:
            with self.send_lock:
                self.request.sendall(encoded + b"\r\n")
        except OSError:
            pass


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the minimal IRC relay used by microirc.py."
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="interface to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port")
    parser.add_argument("--encoding", default="utf-8", help="IRC wire encoding")
    parser.add_argument("-v", "--verbose", action="store_true", help="show IRC traffic")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("microirc_server: port must be in the range 1-65535")
    try:
        with IRCRelayServer(
            (args.host, args.port), encoding=args.encoding, verbose=args.verbose
        ) as server:
            host, port = server.server_address[:2]
            if isinstance(host, bytes):
                host = host.decode("ascii", "replace")
            print(f"microirc server listening on {host}:{port}")
            server.serve_forever()
    except OSError as exc:
        print(f"microirc_server: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nmicroirc_server: stopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
