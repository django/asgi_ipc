import os
from setuptools import setup

__version__ = '1.2.0'

# We use the README as the long_description
readme_path = os.path.join(os.path.dirname(__file__), "README.rst")


setup(
    name='asgi_ipc',
    version=__version__,
    url='http://github.com/django/asgi_ipc/',
    author='Django Software Foundation',
    author_email='foundation@djangoproject.com',
    description='Posix IPC-backed ASGI channel layer implementation',
    long_description=open(readme_path).read(),
    license='BSD',
    zip_safe=False,
    py_modules=["asgi_ipc"],
    include_package_data=True,
    install_requires=[
        'six',
        'posix_ipc>=1.0.0',
        'msgpack-python',
        'asgiref>=1.0.0',
    ]
)
