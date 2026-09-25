from setuptools import find_packages, setup

setup(
    name="complaint-core",
    version="0.1.0",
    description="环境投诉资料基础服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
