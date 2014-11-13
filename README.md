Ecospold2Matrix
===============

A Python class to parse an ecospold2 life cycle assessment dataset and arrange it as matrices for further calculations.

It can recast an Ecoinvent 3 database into a Leontief coefficient matrix with extensions, or it can arrange the unallocated Ecoinvent data as Supply and Use tables (SUT).


Basic functionality
-------------------

- Conveniently store and log all parameters relevant to the data manipulation
- Perform basic quality checks on data, and fix some potential issues
- Arrange allocated data as Leontief technical coefficient matrix, with environmental extensions and labels
- Arrange unallocated data as SUT
- Optionally, change sign conventions for waste treatment
- Optionally, scale elementary and intermediate flows to recorded production volumes
- Save matrices to various different formats



Two main methods
----------------
	import ecospold2matrix as e2m

	# Define parser object, with default and project-specific parameters
	parser = e2m.Ecospold2Matrix('/database/location/', project_name='eco31_cons', positive_waste=True)

	# Assemble matrices, including scaled-up flow matrices, and save to csv-files
	parser.ecospold_to_Leontief(fileformats=['csv'], with_absolute_flows=True)

Short Demo
----------
Have a look at this [Ipython notebook for a demo of typical usage](http://nbviewer.ipython.org/github/majeau-bettez/ecospold2matrix/blob/master/doc/ecospold2matrix_demo.ipynb)


Dependencies
------------

- Python 3
- Pandas
- Numpy
- Scipy
