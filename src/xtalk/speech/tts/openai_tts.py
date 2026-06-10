import os
from typing import Any, AsyncIterator, Optional
import requests
import aiohttp
import numpy as np
import soundfile as sf
import soxr
import io
from ..interfaces import TTS


class OpenAITTS(TTS):
    """
    OpenAI-compatible Text-to-Speech implementation.

    This class provides text-to-speech functionality using any OpenAI-compatible TTS API
    (e.g., standard OpenAI, vLLM, or custom wrappers).
    """

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: Optional[str] = None,
        model: str = "tts-1",
        voice: str = "alloy",
        response_format: str = "pcm",  # commonly pcm, mp3, opus
        sample_rate: int = 48000,
        speed: float = 1.0,
        timeout: float = 30.0,
    ):
        """
        Initialize OpenAITTS.

        Args:
            base_url (str): Base URL of the OpenAI-compatible API.
            api_key (Optional[str]): API key. If None, will load from OPENAI_API_KEY env var.
            model (str): Model name (e.g., tts-1, tts-1-hd, or custom).
            voice (str): Voice ID.
            response_format (str): Desired output format from the API.
            sample_rate (int): Desired output sample rate.
            speed (float): Playback speed.
            timeout (float): Request timeout.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.voice = voice
        self.response_format = response_format
        self._sample_rate = sample_rate
        self.speed = speed
        self._timeout = timeout

    def clone(self):
        return OpenAITTS(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            voice=self.voice,
            response_format=self.response_format,
            sample_rate=self._sample_rate,
            speed=self.speed,
            timeout=self._timeout,
        )

    def set_voice(self, voice_names: list[str]) -> None:
        """Set the active voice."""
        if not voice_names:
            raise ValueError("voice_names cannot be empty")
        self.voice = voice_names[0]

    @staticmethod
    def _float32_to_pcm_bytes(audio_float: np.ndarray) -> bytes:
        """float32 ndarray [-1, 1] -> PCM int16 bytes."""
        audio_int16 = np.clip(audio_float * 32768.0, -32768, 32767).astype(np.int16)
        return audio_int16.tobytes()

    def _resample_bytes(self, raw_bytes: bytes) -> bytes:
        """Decode and resample audio bytes to target sample rate PCM int16.

        Handles both WAV/FLAC/MP3 (decoded via soundfile) and raw PCM.
        The remote Index-TTS server returns WAV, so we always try soundfile
        first and fall back to raw-PCM interpretation only if it fails.
        """
        try:
            # Try decoding as a container format first (WAV, FLAC, MP3 …)
            audio, src_sr = sf.read(io.BytesIO(raw_bytes), dtype="float32")
        except Exception:
            # Fall back to raw PCM interpretation (OpenAI /v1/audio/speech with format=pcm)
            src_sr = 24000
            audio_int16 = np.frombuffer(raw_bytes, dtype=np.int16)
            audio = audio_int16.astype(np.float32) / 32768.0

        if src_sr != self._sample_rate:
            audio = soxr.resample(audio, src_sr, self._sample_rate)

        return self._float32_to_pcm_bytes(audio)

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _get_payload(self, text: str) -> dict:
        payload: dict = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
        }
        # Only include optional fields if explicitly set (some servers reject unknown params)
        if self.response_format:
            payload["response_format"] = self.response_format
        if self.speed != 1.0:
            payload["speed"] = self.speed
        return payload

    def synthesize(self, text: str) -> bytes:
        """Convert text to speech."""
        url = f"{self.base_url}/audio/speech"
        try:
            response = requests.post(
                url,
                headers=self._get_headers(),
                json=self._get_payload(text),
                timeout=self._timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"HTTP {response.status_code}: {response.text[:200]}"
                )
            return self._resample_bytes(response.content)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Failed to synthesize speech with OpenAITTS: {exc}"
            ) from exc

    async def async_synthesize(self, text: str, **_: Any) -> bytes:
        url = f"{self.base_url}/audio/speech"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session:
                async with session.post(
                    url,
                    headers=self._get_headers(),
                    json=self._get_payload(text),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
                    content = await resp.read()
                    return self._resample_bytes(content)
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"Failed to synthesize speech with OpenAITTS: {exc}"
            ) from exc

    async def async_synthesize_stream(
        self, text: str, **_: Any
    ) -> AsyncIterator[bytes]:
        url = f"{self.base_url}/audio/speech"
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout, trust_env=True
            ) as session:
                async with session.post(
                    url,
                    headers=self._get_headers(),
                    json=self._get_payload(text),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")

                    if self.response_format == "pcm":
                        async for chunk in resp.content.iter_chunked(4096):
                            if chunk:
                                yield chunk
                        return

                    buffer = bytearray()
                    async for chunk in resp.content.iter_chunked(4096):
                        if chunk:
                            buffer.extend(chunk)
                    if buffer:
                        yield self._resample_bytes(bytes(buffer))
        except aiohttp.ClientError as exc:
            raise RuntimeError(
                f"Failed to stream speech with OpenAITTS: {exc}"
            ) from exc
