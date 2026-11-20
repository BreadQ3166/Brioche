"""Only ship the active capture backend; Linux ctypes probes must not run on Mac."""
hiddenimports = ['mss.darwin']
excludedimports = ['mss.linux', 'mss.windows']
