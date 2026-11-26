"""
Заплатка для discord.py 2.7.1 + davey 0.2.x.

davey 0.2.0 больше не переключает ключи шифрования сам в process_commit/process_welcome:
нужно вызывать prepare_transition() и execute_transition(). discord.py 2.7.1 этого не делает,
поэтому бот "играет", а слушатели ничего не слышат. Здесь мы добавляем эти вызовы.
Для davey 0.1.x заплатка не включается.
"""
import logging
import struct

log = logging.getLogger("dave-fix")


def apply() -> None:
    try:
        import davey
    except ImportError:
        return
    if not hasattr(davey.DaveSession, "prepare_transition"):
        log.info("davey %s — заплатка не нужна", getattr(davey, "__version__", "?"))
        return

    from discord.gateway import DiscordVoiceWebSocket
    from discord.voice_state import VoiceConnectionState

    async def received_binary_message(self, msg: bytes) -> None:
        self.seq_ack = struct.unpack_from(">H", msg, 0)[0]
        op = msg[2]
        state = self._connection
        session = state.dave_session
        if session is None:
            return

        if op == self.MLS_EXTERNAL_SENDER:
            session.set_external_sender(msg[3:])
        elif op == self.MLS_PROPOSALS:
            optype = msg[3]
            result = session.process_proposals(
                davey.ProposalsOperationType.append if optype == 0 else davey.ProposalsOperationType.revoke, msg[4:]
            )
            if isinstance(result, davey.CommitWelcome):
                await self.send_binary(
                    DiscordVoiceWebSocket.MLS_COMMIT_WELCOME,
                    result.commit + result.welcome if result.welcome else result.commit,
                )
        elif op in (self.MLS_ANNOUNCE_COMMIT_TRANSITION, self.MLS_WELCOME):
            transition_id = struct.unpack_from(">H", msg, 3)[0]
            try:
                if op == self.MLS_WELCOME:
                    session.process_welcome(msg[5:])
                else:
                    session.process_commit(msg[5:])
                session.prepare_transition(transition_id, state.dave_protocol_version)
                if transition_id == 0:
                    session.execute_transition(0)  # переход 0 выполняется сразу
                else:
                    state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                    await self.send_transition_ready(transition_id)
                log.debug("DAVE: переход %d подготовлен, ready=%s", transition_id, session.ready)
            except Exception:  # noqa: BLE001
                log.warning("DAVE: не удалось обработать commit/welcome %d", transition_id, exc_info=True)
                await state._recover_from_invalid_commit(transition_id)

    original_execute = VoiceConnectionState._execute_transition

    async def _execute_transition(self, transition_id: int) -> None:
        if self.dave_session is not None:
            try:
                self.dave_session.execute_transition(transition_id)
            except Exception:  # noqa: BLE001
                log.debug("DAVE: execute_transition(%d) не выполнен", transition_id, exc_info=True)
        await original_execute(self, transition_id)

    DiscordVoiceWebSocket.received_binary_message = received_binary_message
    VoiceConnectionState._execute_transition = _execute_transition
    log.info("Включена заплатка шифрования голоса для davey %s", getattr(davey, "__version__", "?"))
