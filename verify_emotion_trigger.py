from lifecycle.emotion.trigger import EmotionTrigger
from lifecycle.emotion.config import load_emotion_config
from lifecycle.emotion.engine import EmotionEngine

def run_tests():
    config = load_emotion_config()
    engine = EmotionEngine(config)
    trigger = EmotionTrigger(engine)

    # 1. Long message
    res = trigger.should_save("a" * 301)
    print(f"Long message: {res}")
    assert res == (True, "long_message", 0.3)

    # 2. Complex question
    res = trigger.should_save("What? How? Why?")
    print(f"Complex question: {res}")
    assert res == (True, "complex_question", 0.4)

    # 3. Exclamation
    res = trigger.should_save("!! !!")
    print(f"Exclamation: {res}")
    assert res == (True, "exclamation", 0.3)

    # 4. Emotional state
    res = trigger.should_save("Hello", emotional_state={"joy": 0.9})
    print(f"High emotion: {res}")
    assert res == (True, "high_emotion", 0.6)

    # 5. State shift
    res = trigger.should_save("Hello", state_delta={"anger": 0.2})
    print(f"State shift: {res}")
    assert res == (True, "state_shift_anger", 0.4)

    # 6. Phrase
    res = trigger.should_save("я тебя люблю")
    print(f"Phrase: {res}")
    assert res == (True, "emotion_love", 0.8)

    # 7. Marker
    res = trigger.should_save("люблю")
    print(f"Marker: {res}")
    assert res == (True, "emotion_love", 0.4)

    # 8. Highest score wins
    res = trigger.should_save("What? How? Why?", emotional_state={"joy": 0.9})
    print(f"Highest score: {res}")
    assert res == (True, "high_emotion", 0.6)

    # 9. No trigger
    res = trigger.should_save("Just a normal message.")
    print(f"No trigger: {res}")
    assert res == (False, "", 0.0)

    print("All manual checks passed!")

if __name__ == "__main__":
    try:
        run_tests()
    except Exception as e:
        print(f"Verification failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
