"""Only the native Mac input backend is used in a Mac application bundle."""
hiddenimports = ['pynput.keyboard._darwin','pynput.mouse._darwin','pynput._util.darwin']
excludedimports = ['pynput.keyboard._xorg','pynput.mouse._xorg','pynput._util.xorg',
                   'pynput.keyboard._win32','pynput.mouse._win32','pynput._util.win32',
                   'pynput.keyboard._uinput','pynput._util.uinput']
