from setuptools import find_packages
from setuptools import setup


setup(
        name = "BAM_Util",
        version     = '0.1.1',
        packages=find_packages(),
        package_data={
            '':['/root/BAM_Tensor/BAM_DataLoader/module/build/BAM_Feature_Store/BAM_Feature_Store.so'] }
)

