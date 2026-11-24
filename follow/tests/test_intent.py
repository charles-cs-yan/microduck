"""Loose sentence → command matching for the laptop recogniser (follow/voice/intent.py)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "voice"))
from intent import intent_of  # noqa: E402


class IntentTest(unittest.TestCase):
    def test_what_the_recogniser_heard_on_the_duck(self):
        # Real transcripts from the duck's mic, 2026-11-24 (voice_eval on a guided recording).
        cases = {
            "less party": "dance", "Let's party": "dance", "shake it": "dance",
            "Last go home": "home", "Let's go home": "home",
            "Let's go follow me": "follow", "follow me": "follow",
            "say hello": "greet", "Look at me": "gaze", "Stop": "stop", "Let's go": "letsgo",
            "Last body": "dance", "last party": "dance", "top": "stop",
            "sit down": "sit", "set down": "sit", "Say down": "sit", "get up": "stand", "stand up": "stand",
            "lie down": "crouch", "lay down": "crouch", "follow me": "follow", "go home": "home",
        }
        for text, label in cases.items():
            self.assertEqual(intent_of(text), label, text)

    def test_ordinary_speech_is_not_a_command(self):
        for text in ("thank everyone welcome to the shore", "Thank you everyone. Welcome to the show.",
                     "", "the go-to market plan", "hell of a night",
                     "everybody please welcome", "on top of that", "nobody",
                     "thank you for sitting with us", "the setup is done", "lights down please"):
            self.assertIsNone(intent_of(text), text)

    def test_specific_beats_general(self):
        self.assertEqual(intent_of("let's go home now"), "home")
        self.assertEqual(intent_of("let's go, follow me"), "follow")


class FuzzyTest(unittest.TestCase):
    def test_bent_spellings_still_land(self):
        for text, label in {"lass party": "dance", "say hallo": "greet", "folow me": "follow",
                            "look add me": "gaze", "sit dawn": "sit", "lye down": "crouch"}.items():
            self.assertEqual(intent_of(text), label, text)

    def test_the_boss_interviews_trigger_nothing(self):
        # Recogniser output of ~3 min of the host's real interview speech (2026-11-25).
        for text in ("slow motion base if I look at it the paper", "Jensen took it away, everything become so fast.",
                     "one step at a time when you go to the top and you see the entire mountain, right?",
                     "and learn how to ask questions", "Well, thank you so much. I think that is very sage advice",
                     "you need that extra expertise, you need that extra help Exactly."):
            self.assertIsNone(intent_of(text), text)


if __name__ == "__main__":
    unittest.main()
