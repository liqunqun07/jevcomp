"""Fallback for older pip/setuptools that cannot read PEP 621 pyproject.toml."""

from setuptools import setup

setup(
    name="jevcomp",
    version="0.1.0",
    description=(
        "Agent-universal context compaction via TypeSafe Jev: deletion instead "
        "of lossy summaries, everything kept stays verbatim."
    ),
    long_description_content_type="text/markdown",
    license="MIT",
    python_requires=">=3.9",
    packages=["jevcomp", "jevcomp.adapters"],
    entry_points={"console_scripts": ["jevcomp=jevcomp.cli:main"]},
)
