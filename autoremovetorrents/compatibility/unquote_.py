def unquote_(string):
    try: # for Python 3
        from urllib.parse import unquote
    except ImportError: # for Python 2.7
        from urllib import unquote

    return unquote(string)
