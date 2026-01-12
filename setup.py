from setuptools import setup, find_packages

setup(
    name="meco-devel",
    version="0.1.0",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    install_requires=[
        "grpcio",
        "protobuf",
        "pyyaml",
        "psutil",
    ],
    python_requires=">=3.10",
)
