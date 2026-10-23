from claude_phone.lights import parse


def kind(text):
    c = parse(text)
    return c and (c.kind, c.value)


def test_on_off_need_a_light_word():
    assert kind("Turn the lights off, please.") == ("off", None)
    assert kind("Lights on.") == ("on", None)
    assert kind("lamps out") == ("off", None)
    assert parse("put me through to the one on main") is None
    assert parse("the frontend") is None


def test_colours_and_modes():
    assert kind("Make it red.") == ("colour", (255, 0, 0))
    assert kind("Party mode!")[0] == "party"
    assert kind("disco")[0] == "party"
    assert kind("focus mode") == ("mode", "focus")
    assert kind("back to normal") == ("mode", "normal")
    assert kind("warm white") == ("kelvin", 2700)


def test_brightness():
    assert kind("lights to 30 percent") == ("brightness", 30)
    assert kind("fifty percent") == ("brightness", 50)
    assert kind("dim the lights")[0] == "dimmer"
    assert kind("lights brighter")[0] == "brighter"
    assert kind("full brightness") == ("brightness", 100)


def test_finished():
    from claude_phone.lights import finished
    for t in ("No.", "Nothing else needed.", "No, that's all.", "That's it, thanks.", "Bye!"):
        assert finished(t), t
    for t in ("Lights on.", "the frontend", "no, make it red"):
        assert not finished(t), t


def test_lights_back_on_is_not_the_backend():
    from claude_phone.lights import mentions_lights
    assert kind("Can you turn the lights back on?") == ("on", None)
    assert mentions_lights("Can you turn the lights back on?")
    assert not mentions_lights("the back end")


def test_follow_up_without_the_word_lights():
    # the operator prefixes "lights" once a lights command has been given
    assert kind("lights Can you turn them back on again?") == ("on", None)
    assert kind("lights Back to normal.") == ("mode", "normal")
    assert kind("lights brighter")[0] == "brighter"
