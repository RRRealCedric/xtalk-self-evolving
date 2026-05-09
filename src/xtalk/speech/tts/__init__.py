__all__ = []

# Dummy TTS for testing
try:
    from .dummy import DummyTTS as DummyTTS

    __all__.append("DummyTTS")
except:
    pass

# CosyVoice (DashScope)
try:
    from .cosyvoice import CosyVoice as CosyVoice

    __all__.append("CosyVoice")
except Exception as e:
    print(f"Failed to import CosyVoice: {e}")

# CosyVoice Local
try:
    from .cosyvoice_local import CosyVoiceLocal as CosyVoiceLocal

    __all__.append("CosyVoiceLocal")
except:
    pass

# IndexTTS
try:
    from .index_tts import IndexTTS as IndexTTS

    __all__.append("IndexTTS")
except:
    pass

# IndexTTS2
try:
    from .index_tts2 import IndexTTS2 as IndexTTS2

    __all__.append("IndexTTS2")
except:
    pass

# Edge TTS
try:
    from .edge_tts import EdgeTTS as EdgeTTS

    __all__.append("EdgeTTS")
except:
    pass

# ElevenLabs TTS
try:
    from .elevenlabs import ElevenLabsTTS as ElevenLabsTTS

    __all__.append("ElevenLabsTTS")
except:
    pass
# GPT-SoVITS
try:
    from .gpt_sovits import GPTSoVITS as GPTSoVITS

    __all__.append("GPTSoVITS")
except:
    pass

# OpenAI TTS
try:
    from .openai_tts import OpenAITTS as OpenAITTS

    __all__.append("OpenAITTS")
except Exception as e:
    print(f"Failed to load OpenAITTS: {e}")
