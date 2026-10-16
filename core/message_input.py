"""Enter message text without triggering the chat's Enter-to-send handler."""


def type_message(editor, message):
    # Templates use literal \n; API quotes can contain actual LF, CRLF or CR.
    # Playwright types raw line breaks as Enter, which can send a partial draft.
    lines = message.replace('\r\n', '\n').replace('\r', '\n').replace(r'\n', '\n').split('\n')
    for index, line in enumerate(lines):
        editor.type(line)
        if index < len(lines) - 1:
            editor.press('Shift+Enter')
