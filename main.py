import logging
from dotenv import load_dotenv
from livekit.agents.voice import MetricsCollectedEvent
from livekit.plugins import noise_cancellation, openai, rime, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
    metrics,
)


load_dotenv()
logger = logging.getLogger("voice-agent")


OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TRANSCRIPT_MODEL = "gpt-4o-transcribe"
SYSTEM_PROMPT = "You are a helpful and friendly voice assistant. Keep your responses concise and conversational."
RIME_MODEL = "arcana"
RIME_SPEAKER = "astra"
INTRO_PHRASE = "Hello! How can I help you today?"


def prewarm(proc: JobProcess):
    """Load the Silero VAD model into shared process memory before any job starts.

    Running this once per worker process avoids reloading the model on every
    job and significantly reduces cold-start latency.

    Args:
        proc: The worker process context used to store shared state.
    """
    proc.userdata["vad"] = silero.VAD.load()


class VoiceAssistant(Agent):
    """LiveKit Agent powered by Rime TTS.

    Configures the LLM system prompt that governs the assistant's personality
    and response style throughout the session.
    """

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)


async def entrypoint(ctx: JobContext):
    """Entry point for each incoming LiveKit job.

    Connects to the room, waits for a participant, then wires together the
    full voice pipeline — STT (OpenAI), LLM (OpenAI), TTS (Rime), VAD
    (Silero), and turn detection (multilingual) — before starting the session
    and greeting the user.

    Args:
        ctx: The job context providing room access and process-level state.
    """
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    await ctx.wait_for_participant()

    rime_tts = rime.TTS(model=RIME_MODEL, speaker=RIME_SPEAKER)
    session = AgentSession(
        stt=openai.STT(
            model=OPENAI_TRANSCRIPT_MODEL,
        ),
        llm=openai.LLM(model=OPENAI_MODEL),
        tts=rime_tts,
        vad=ctx.proc.userdata["vad"],
        turn_detection=MultilingualModel(),
    )
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        """Log and accumulate token/cost metrics for each pipeline event."""
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def log_usage():
        """Log a cumulative usage summary when the session shuts down."""
        summary = usage_collector.get_summary()
        logger.info("Usage: %s", summary)

    ctx.add_shutdown_callback(log_usage)

    await session.start(
        room=ctx.room,
        agent=VoiceAssistant(),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVC()
        ),
    )
    await session.say(INTRO_PHRASE)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )