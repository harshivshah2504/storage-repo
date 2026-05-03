from pathlib import Path

from setuptools import find_packages, setup


readme_path = Path(__file__).with_name("README.md")
README = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""


setup(
    name="github-drive",
    version="0.2.0",
    description="Upload and restore arbitrary files as GitHub Release archives",
    long_description=README,
    long_description_content_type="text/markdown",
    packages=find_packages(),
    include_package_data=True,
    package_data={"github_drive": ["templates/*.html", "static/*"]},
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "github-drive = github_drive.main:run",
        ],
    },
    install_requires=[
        "Flask",
        "requests>=2.31.0",
        "PyCryptodome==3.17.0",
        "Pillow>=10.0.0",
        "psycopg[binary,pool]>=3.1",
    ],
    classifiers=[
        "Topic :: Internet :: WWW/HTTP",
        "Development Status :: 4 - Beta",
        "Environment :: Console",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: POSIX",
        "Operating System :: Microsoft :: Windows",
        "Programming Language :: Python :: 3",
    ],
)
