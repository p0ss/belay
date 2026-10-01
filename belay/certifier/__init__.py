"""
The black-box certifier. It talks to the detector over HTTP and reads alert
logs; it never imports HAT, torch or transformers, and never loads a model.
"""
