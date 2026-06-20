def make_widget(name):
    return {"name": name, "enabled": True}


def disable_widget(widget):
    return {**widget, "enabled": False}
