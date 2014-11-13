""" ecospold2matrix - Class for recasting ecospold2 dataset in matrix form.

The module provides function to parse ecospold2 data, notably ecoinvent 3, as
Leontief A-matrix and extensions, or alternatively as supply and use tables for
the unallocated version of ecoinvent.

:PythonVersion:  3
:Dependencies: pandas 0.14.1 or more recent, scipy, numpy, lxml and xml

License: BDS

Authors:
    Guillaume Majeau-Bettez
    Konstantin Stadler

Credits:
    This module re-uses/adapts code from brightway2data, more specifically the
    Ecospold2DataExtractor class in import_ecospold2.py, changeset:
    271:7e67a75ed791; Wed Sep 10; published under BDS-license:

        Copyright (c) 2014, Chris Mutel and ETH Zürich
        Neither the name of ETH Zürich nor the names of its contributors may be
        used to endorse or promote products derived from this software without
        specific prior written permission.  THIS SOFTWARE IS PROVIDED BY THE
        COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED
        WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
        MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN
        NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY
        DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
        DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
        OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
        HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
        STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
        IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
        POSSIBILITY OF SUCH DAMAGE.


"""

import os
import glob
import subprocess
from lxml import objectify
import xml.etree.ElementTree as ET
import pandas as pd
import numpy as np
import scipy.sparse
import scipy.io
import logging
import pickle
import csv
import shelve
import hashlib
# pylint: disable-msg=C0103


class Ecospold2Matrix(object):
    """
    Defines a parser object that holds all project parameters and processes the
    ecospold-formatted data into matrices of choice.

    The two main functions of this class are ecospold_to_Leontief() and
    ecospold_to_sut()

    """

    # Some hardcoded stuff
    __PRE = '{http://www.EcoInvent.org/EcoSpold02}'
    __ELEXCHANGE = 'ElementaryExchanges.xml'
    __INTERMEXCHANGE = 'IntermediateExchanges.xml'
    __ACTIVITYINDEX = 'ActivityIndex.xml'
    rtolmin = 1e-16  # 16 significant digits being roughly the limit of float64

    def __init__(self, sys_dir, project_name, out_dir='.', lci_dir=None,
                 positive_waste=False, prefer_pickles=False, nan2null=False,
                 save_interm=True, PRO_order=['ISIC', 'activityName'],
                 STR_order=['compartment', 'subcompartment', 'name']):

        """ Defining an ecospold2matrix object, with key parameters that
        determine how the data will be processes.

        Args:
        -----

        * sys_dir: directory containing the system description,i.e., ecospold
                   dataset and master XML files

        * project_name: Name used to log progress and save results

        * out_dir: Directory where to save result matrices and logs

        * lci_dir: Directory where official cummulative LCI ecospold files are

        * positive_waste: Whether or not to change sign convention and make
                          waste flows positive
                          [default false]

        * prefer_pickles: If sys_dir contains pre-processed data in form of
                          pickle-files, whether or not to use those
                          [Default: False, don't use]

        * nan2null: Whether or not to replace Not-a-Number by 0.0
                    [Default: False, don't replace anything]

        * save_interm: Whether or not to save intermediate results as pickle
                       files for potential re-use
                       [Default: True, do it]

        * PRO_order: List of meta-data used for sorting processes in the
                     different matrices.
                     [Default: first sort by order of ISIC code, then, within
                               each code, by order of activity name]

        * PRO_order: List of meta-data used for sorting stressors (elementary
                     flows) in the different matrices.
                     [Default: first sort by order of compartment,
                               subcompartment and then by name]


        Main functions and worflow:
        ---------------------------

        self.ecospold_to_Leontief(): Turn ecospold files into Leontief matrix
        representation

            * Parse ecospold files, get products, activities, flows, emissions
            * If need be, correct inconsistencies in system description
            * After corrections, create "final" labels for matrices
            * Generate symmetric, normalized system description (A-matrix,
              extension F-matrix)
            * Save to file (many different formats)
            * Optionally, read cummulative lifecycle inventories (slow) and
              compare to calculated LCI for sanity testing

        self.ecospold_to_sut(): Turn unallocated ecospold into Suppy and Use
        Tables

            * Parse ecospold files, get products, activities, flows, emissions
            * Organize in supply and use
            * optionally, aggregate sources to generate a fully untraceable SUT
            * Save to file


        """

        # INTERMEDIATE DATA/RESULTS, TO BE GENERATED BY OBJECT METHODS
        self.products = None            # products, with IDs and descriptions
        self.activities = None          # activities, w IDs and description
        self.inflows = None             # intermediate-exchange input flows
        self.outflows = None            # intermediate-exchange output flows
        self.elementary_flows = None    # elementary flows
        self.q = None                   # total supply of each product

        # FINAL VARIABLES: SYMMETRIC SYSTEM, NORMALIZED AND UNNORMALIZED
        self.PRO = None             # Process labels, rows/cols of A-matrix
        self.STR = None             # Factors labels, rows extensions
        self.A = None               # Normalized Leontief coefficient matrix
        self.F = None               # Normalized factors of production,i.e.,
                                    #       elementary exchange coefficients
        self.Z = None               # Intermediate unnormalized process flows
        self.G_pro = None           # Unnormalized Process factor requirements

        # Final variables, unallocated and unnormalized inventory
        self.U = None               # Table of use of products by activities
        self.V = None               # Table of supply of product by activities
                                    #      (ammounts for which use is recorded)
        self.G_act = None           # Table of factor use by activities
        self.V_prodVol = None       # Table of supply production volumes
                                    #       (potentially to rescale U, V and G)

        # QUALITY CHECKS VARIABLES, TO BE GENERATED BY OBJECT METHODS.
        self.E = None                   # cummulative LCI matrix (str x pro)
        self.unsourced_flows = None     # product flows without clear source
        self.missing_activities = None  # cases of no incomplete dataset, i.e.,
                                        #   no producer for a product

        # PROJECT NAME AND DIRECTORIES, FROM ARGUMENTS
        self.sys_dir = os.path.abspath(sys_dir)
        self.project_name = project_name
        self.out_dir = os.path.abspath(out_dir)
        if lci_dir:
            self.lci_dir = os.path.abspath(lci_dir)
        else:
            self.lci_dir = lci_dir

        # PROJECT-WIDE OPTIONS
        self.positive_waste = positive_waste
        self.prefer_pickles = prefer_pickles
        self.nan2null = nan2null
        self.save_interm = save_interm
        self.PRO_order = PRO_order
        self.STR_order = STR_order

        # CREATE DIRECTORIES IF NOT IN EXISTENCE
        if out_dir and not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)
        self.log_dir = os.path.join(self.out_dir, self.project_name + '_log')

        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        # DEFINE LOG TOOL
        self.log = logging.getLogger(self.project_name)
        self.log.setLevel(logging.INFO)
        self.log.handlers = []                            # reset handlers
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        fh = logging.FileHandler(os.path.join(self.log_dir,
                                              project_name + '.log'))
        fh.setLevel(logging.INFO)
        aformat = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        formatter = logging.Formatter(aformat)
        ch.setFormatter(formatter)
        fh.setFormatter(formatter)
        self.log.addHandler(ch)
        self.log.addHandler(fh)

        # RECORD OBJECT/PROJECT IDENTITY TO LOG
        self.log.info('Ecospold2Matrix Processing')
        try:
            gitcommand = ["git", "log", "--pretty=format:%H", "-n1"]
            githash = subprocess.check_output(gitcommand).decode("utf-8")
            self.log.info("Current git commit: {}".format(githash))
        except:
            pass
        self.log.info('Project name: ' + self.project_name)

        # RECORD PROJECT PARAMETERS TO LOG
        self.log.info('Unit process and Master data directory: ' + sys_dir)
        self.log.info('Data saved in: ' + self.out_dir)
        if self.lci_dir:
            self.log.info('Official rolled-up life cycle inventories in: ' +
                          self.lci_dir)
        if self.positive_waste:
            self.log.info('Sign conventions changed to make waste flows '
                          'positive')
        if self.prefer_pickles:
            self.log.info('When possible, loads pickled data instead of'
                          ' parsing ecospold files')
        if self.nan2null:
            self.log.info('Replace Not-a-Number instances with 0.0 in all'
                          ' matrices')
        if self.save_interm:
            self.log.info('Pickle intermediate results to files')
        self.log.info('Order processes based on: ' +
                      ', '.join([i for i in self.PRO_order]))
        self.log.info('Order elementary exchanges based on: ' +
                      ', '.join([i for i in self.STR_order]))

    # =========================================================================
    # MAIN FUNCTIONS
    def ecospold_to_Leontief(self, fileformats=None, with_absolute_flows=False,
                             lci_check=False, rtol=1e-2, atol=1e-5, imax=3):
        """ Recasts an full ecospold dataset into normalized symmetric matrices

        Args:
        -----

        * fileformats : List of file formats in which to save data
                        [Default: None, save to all possible file formats]
                         Options: 'Pandas'       --> pandas dataframes
                                  'SparsePandas' --> sparse pandas dataframes
                                  'SparseMatrix' --> scipy AND matlab sparse
                                  'csv'          --> text with separator =  '|'

        * with_absolut_flow: If true, produce not only coefficient matrices (A
                             and F) but also scale them up to production
                             volumes to get absolute flows in separate
                             matrices.  [default: false]

        * lci_check : If true, and if lci_dir is not None, parse cummulative
                      lifecycle inventory data as self.E matrix (str x pro),
                      and use it for sanity check against calculated
                      cummulative LCI

        * rtol : Initial (max) relative tolerance for comparing E with
                 calculated E

        * atol : Initial (max) absolute tolerance for comparing E with
                 calculated E

        Generates:
        ----------

        * Intermediate data: products, activities, flows, labels
        * A matrix: Normalized, intermediate exchange Leontief coefficients
                    (pro x pro)

        * F matrix: Normalized extensions, factor requirements (elementary
                    exchanges) for each process (str x pro)

        * E matrix: [optionally] cummulative normalized lci data (str x pro)
                                 (for quality check)

        Returns:
        -------

        * None, save all matrices in the object, and to file

        """

        # Read in system description
        self.extract_products()
        self.extract_activities()
        self.get_flows()
        self.get_labels()

        # Clean up if necessary
        self.__find_unsourced_flows()
        if self.unsourced_flows is not None:
            self.__fix_flow_sources()
        self.__fix_missing_activities()

        # Once all is well, add extra info to PRO and STR, and order nicely
        self.complement_labels()

        # Finally, assemble normalized, symmetric matrices
        self.build_AF()

        if with_absolute_flows:
            self.scale_up_AF()

        # Save system to file
        self.save_system(fileformats)

        # Read/load lci cummulative emissions and perform quality check
        if lci_check:
            self.get_cummulative_lci()
            self.cummulative_lci_check(rtol, atol, imax)

        self.log.info('Done running ecospold2matrix.ecospold_to_Leontief')

    def ecospold_to_sut(self, fileformats=None, make_untraceable=False):
        """ Recasts an unallocated ecospold dataset into supply and use tables

        Args:
        -----

        * fileformats : List of file formats in which to save data
                        [Default: None, save to all possible file formats]
                         Options: 'Pandas'       --> pandas dataframes
                                  'SparsePandas' --> sparse pandas dataframes,
                                  'SparseMatrix' --> scipy AND matlab sparse
                                  'csv'          --> text files

        * make_untraceable: Whether or not to aggregate away the source
                            activity dimension, yielding a use table in which
                            products are no longer linked to their providers
                            [default: False; don't do it]


        Generates:
        ----------

        * Intermediate data:  Products, activities, flows, labels

        * V table             Matrix of supply of product by activities

        * U table             Matrix of use of products by activities
                                  (recorded for a given supply amount, from V)

        * G_act               Matrix of factor use by activities
                                  (recorded for a given supply amount, from V)

        * V_prodVol           Matrix of estimated real production volumes,
                                  arranged as suply table (potentially useful
                                  to rescale U, V and G)

        Returns:
        -------

        * None, save all matrices in the object, and to file

        """

        # Extract data on producs and activities
        self.extract_products()
        self.extract_activities()

        # Extract or load data on flows and labels
        self.get_flows()
        self.get_labels()
        self.complement_labels()

        # Arrange as supply and use
        self.build_sut(make_untraceable)

        # Save to file
        self.save_system(fileformats)

        self.log.info("Done running ecospold2matrix.ecospold_to_sut")

    # =========================================================================
    # INTERMEDIATE WRAPPER METHODS: parse or load data + pickle results or not
    def get_flows(self):
        """ Wrapper: load from pickle or call extract_flows() to read ecospold
        files.


        Behavious determined by:
        ------------------------
            prefer_pickles: Whether or not to load flow lists from previous run
                            instead of (re)reading XML Ecospold files

            save_interm: Whether or not to pickle flows to file for use in
                         another project run.

        Generates:
        ----------
            self.inflows
            self.outflows
            self.elementary_flows

        Returns:
        --------
            None, only defines within object
        """

        filename = os.path.join(self.sys_dir, 'flows.pickle')

        # EITHER LOAD FROM PREVIOUS ROUND...
        if self.prefer_pickles and os.path.exists(filename):

            # Read all flows
            with open(filename, 'rb') as f:
                [self.inflows,
                 self.elementary_flows,
                 self.outflows] = pickle.load(f)

                # Log event
                sha1 = self.__hash_file(f)
                msg = "{} loaded from {} with SHA-1 of {}"
                self.log.info(msg.format('Flows', filename, sha1))

        # ...OR EXTRACT FROM ECOSPOLD DATA..
        else:

            self.extract_flows()

            # optionally, pickle for further use
            if self.save_interm:
                with open(filename, 'wb') as f:
                    pickle.dump([self.inflows,
                                 self.elementary_flows,
                                 self.outflows], f)

                # Log event
                sha1 = self.__hash_file(filename)
                msg = "{} saved in {} with SHA-1 of {}"
                self.log.info(msg.format('Flows', filename, sha1))

    def get_labels(self):
        """
        Wrapper: load from pickle, or call methods to build labels from scratch


        Behaviour determined by:
        ------------------------

        * prefer_pickles: Whether or not to load flow lists from previous run
                          instead of (re)reading XML Ecospold files
        * save_interm: Whether or not to pickle flows to file for use in
                       another project run.

        Generates:
        ----------

        * PRO: metadata on each process, i.e. production of each product
               by each activity.
        * STR: metadata on each stressor (or elementary exchange, factor of
               production)

        Returns:
        --------
        * None, only defines within object

        NOTE:
        -----

        * At this stage, labels are at the strict minimum (ID, name) to
          facilitate the addition of new processes or stressors, if need be, to
          "patch" inconsistencies in the dataset. Once all is sorted out, more
          data from product, activities, and elementary_flow descriptions are
          added to the labels in self.complement_labels()



        """
        filename = os.path.join(self.sys_dir, 'rawlabels.pickle')

        # EITHER LOAD FROM PREVIOUS ROUND...
        if self.prefer_pickles and os.path.exists(filename):

            # Load from pickled file
            with open(filename, 'rb') as f:
                self.PRO, self.STR = pickle.load(f)

                # Log event
                sha1 = self.__hash_file(f)
                msg = "{} loaded from {} with SHA-1 of {}"
                self.log.info(msg.format('Labels', filename, sha1))

        # OR EXTRACT FROM ECOSPOLD DATA...
        else:

            self.build_PRO()
            self.build_STR()

            # and optionally pickle for further use
            if self.save_interm:
                with open(filename, 'wb') as f:
                    pickle.dump([self.PRO, self.STR], f)

                # Log event
                sha1 = self.__hash_file(filename)
                msg = "{} saved in {} with SHA-1 of {}"
                self.log.info(msg.format('Labels', filename, sha1))

    def get_cummulative_lci(self):
        """ Wrapper: load from pickle or call build_E() to read ecospold files.

        Behaviour determined by:
        ------------------------

        * prefer_pickles: Whether or not to load flow lists from previous run
                          instead of (re)reading XML Ecospold files
        * save_interm: Whether or not to pickle flows to file for use in
                       another project run.

        * lci_dir: Directory where cummulative LCI ecospold are

        Generates:
        ----------
        * E: cummulative LCI emissions matrix

        Returns:
        --------
        * None, only defines within object

        """

        filename = os.path.join(self.lci_dir, 'lci.pickle')

        # EITHER LOAD FROM PREVIOUS ROUND...
        if self.prefer_pickles and os.path.exists(filename):
            with open(filename, 'rb') as f:
                self.E = pickle.load(f)

                # log event
                sha1 = self.__hash_file(f)
                msg = "{} loaded from {} with SHA-1 of {}"
                self.log.info(msg.format('Cummulative LCI', filename, sha1))

        # OR BUILD FROM ECOSPOLD DATA...
        else:
            self.build_E()

            # optionally, pickle for further use
            if self.save_interm:
                with open(filename, 'wb') as f:
                    pickle.dump(self.E, f)

                # log event
                sha1 = self.__hash_file(filename)
                msg = "{} saved in {} with SHA-1 of {}"
                self.log.info(msg.format('Cummulative LCI', filename, sha1))

    # =========================================================================
    # PARSING METHODS: the hard work with xml files
    def extract_products(self):
        """ Parses INTERMEDIATEEXCHANGE file to extract core data on products:
        Id's, name, unitID, unitName.

        Args: None
        ----

        Returns: None
        -------

        Generates: self.products
        ----------

        Credit:
        ------
        This function incorporates/adapts code from Brightway2data, i.e., the
        method extract_technosphere_metadata from class Ecospold2DataExtractor
        """

        # The file to parse
        fp = os.path.join(self.sys_dir, 'MasterData', self.__INTERMEXCHANGE)
        assert os.path.exists(fp), "Can't find " + self.__INTERMEXCHANGE

        def extract_metadata(o):
            """ Subfunction to get the data from lxml root object """

            # Get list of id, name, unitId, and unitName for all intermediate
            # exchanges
            return {'productName': o.name.text,
                    'unitName': o.unitName.text,
                    'productId': o.get('id'),
                    'unitId': o.get('unitId')}

        # Parse XML file
        with open(fp) as fh:
            root = objectify.parse(fh).getroot()
            pro_list = [extract_metadata(ds) for ds in root.iterchildren()]

            # Convert this list into a dataFrame
            self.products = pd.DataFrame(pro_list)
            self.products.index = self.products['productId']

        # Log event
        sha1 = self.__hash_file(fp)
        msg = "Products extracted from {} with SHA-1 of {}"
        self.log.info(msg.format(self.__INTERMEXCHANGE, sha1))

    def extract_activities(self):
        """ Parses ACTIVITYINDEX file to extract core data on activities:
        Id's, activity type, startDate, endDate

        Args: None
        ----

        Returns: None
        --------

        Generates: self.activities
        ---------
        """

        # Parse XML file describing activities
        activity_file = os.path.join(self.sys_dir,
                                     'MasterData',
                                     self.__ACTIVITYINDEX)
        root = ET.parse(activity_file).getroot()

        # Get list of activities and their core attributes
        act_list = []
        for act in root:
            act_list.append([act.attrib['id'],
                             act.attrib['specialActivityType'],
                             act.attrib['startDate'],
                             act.attrib['endDate']])

        # Remove any potential duplicates
        act_list, _, _, _ = self.__deduplicate(act_list, 0, 'activity_list')

        # Convert to dataFrame
        self.activities = pd.DataFrame(act_list,
                                       columns=('activityId',
                                                'activityType',
                                                'startDate',
                                                'endDate'),
                                       index=[row[0] for row in act_list])
        self.activities['activityType'
                       ] = self.activities['activityType'].astype(int)

        # Log event
        sha1 = self.__hash_file(activity_file)
        msg = "{} extracted from {} with SHA-1 of {}"
        self.log.info(msg.format('Activities', self.__ACTIVITYINDEX, sha1))

    def extract_flows(self):
        """ Extracts of all intermediate and elementary flows

        Args: None
        ----

        Returns: None
        -------

        Generates:
        ----------
            self.inflows:           normalized product (intermediate) inputs
            self.elementary_flows:  normalized elementary flows
            self.outflows:          normalized product (intermediate) outputs

        """
        # Initialize empty lists
        inflow_list = []
        outflow_list = []
        elementary_flows = []

        # Get list of ecoSpold files to process
        data_folder = os.path.join(self.sys_dir, 'datasets')
        spold_files = glob.glob(os.path.join(data_folder, '*.spold'))

        # Log event
        self.log.info('Processing {} files in {}'.format(len(spold_files),
                                                         data_folder))

        # ONE FILE AT A TIME
        for sfile in spold_files:

            # Get activityId from file name
            current_file = os.path.basename(sfile)
            current_id = os.path.splitext(current_file)[0]

            # For each file, find flow data
            root = ET.parse(sfile).getroot()
            child_ds = root.find(self.__PRE + 'childActivityDataset')
            if child_ds is None:
                child_ds = root.find(self.__PRE + 'activityDataset')
            flow_ds = child_ds.find(self.__PRE + 'flowData')

            # GO THROUGH EACH FLOW IN TURN
            for entry in flow_ds:

                # Get magnitude of flow
                try:
                    _amount = float(entry.attrib.get('amount'))
                except:
                    # Get ID of failed amount
                    _fail_id = entry.attrib.get('elementaryExchangeId',
                                                'not found')
                    if _fail_id == 'not found':
                        _fail_id = entry.attrib.get('intermediateExchangeId',
                                                    'not found')

                    # Log failure
                    self.log.warn("Parser warning: flow in {0} cannot be"
                                  " converted' 'to float. Id: {1} - amount:"
                                  " {2}".format(str(current_file),
                                                _fail_id,
                                                _amount))
                    continue

                if _amount == 0:   # Ignore entries of magnitude zero
                    continue

                #  GET OBJECT, DESTINATION AND/OR ORIGIN OF EACH FLOW

                # ... for elementary flows
                if entry.tag == self.__PRE + 'elementaryExchange':
                    elementary_flows.append([
                                current_id,
                                entry.attrib.get('elementaryExchangeId'),
                                _amount])

                elif entry.tag == self.__PRE + 'intermediateExchange':

                    # ... or product use
                    if entry.find(self.__PRE + 'inputGroup') is not None:
                        inflow_list.append([
                                current_id,
                                entry.attrib.get('activityLinkId'),
                                entry.attrib.get('intermediateExchangeId'),
                                _amount])

                    # ... or product supply.
                    elif entry.find(self.__PRE + 'outputGroup') is not None:
                        outflow_list.append([
                                current_id,
                                entry.attrib.get('intermediateExchangeId'),
                                _amount,
                                entry.attrib.get('productionVolumeAmount'),
                                entry.find(self.__PRE + 'outputGroup').text])

        # Check for duplicates in outputflows
        #   there should really only be one output flow per activity

        outflow_list, _, _, _ = self.__deduplicate(outflow_list,
                                                   0,
                                                   'outflow_list')

        # CONVERT TO DATAFRAMES

        self.inflows = pd.DataFrame(inflow_list, columns=['fileId',
                                                          'sourceActivityId',
                                                          'productId',
                                                          'amount'])

        self.elementary_flows = pd.DataFrame(elementary_flows,
                                             columns=['fileId',
                                                      'elementaryExchangeId',
                                                      'amount'])

        out = pd.DataFrame(outflow_list,
                           columns=['fileId',
                                    'productId',
                                    'amount',
                                    'productionVolume',
                                    'outputGroup'],
                           index=[row[0] for row in outflow_list])
        out['productionVolume'] = out['productionVolume'].astype(float)
        out['outputGroup'] = out['outputGroup'].astype(int)
        self.outflows = out

    def build_STR(self):
        """ Parses ElementaryExchanges.xml to builds stressor labels

        Args: None
        ----

        Behaviour influenced by:
        ------------------------

        * self.STR_order: Determines how labels are ordered

        Returns: None
        -------

        Generates: self.STR:    DataFrame with stressor Id's for index
        ----------

        Credit:
        -------

        This function incorporates/adapts code from Brightway2data, that is,
        the classmethod extract_biosphere_metadata from Ecospold2DataExtractor

        """

        # File to parse
        fp = os.path.join(self.sys_dir, 'MasterData', self.__ELEXCHANGE)
        assert os.path.exists(fp), "Can't find ElementaryExchanges.xml"

        def extract_metadata(o):
            """ Subfunction to extract data from lxml root object """
            return {
                'id': o.get('id'),
                'name': o.name.text,
                'unit': o.unitName.text,
                'casNumber': o.get('casNumber'),
                'compartment': o.compartment.compartment.text,
                'subcompartment': o.compartment.subcompartment.text
            }

        # Extract data from file
        with open(fp) as fh:
            root = objectify.parse(fh).getroot()
            el_list = [extract_metadata(ds) for ds in root.iterchildren()]

        # organize in pandas DataFrame
        STR = pd.DataFrame(el_list)
        STR.index = STR['id']
        STR = STR.reindex_axis(['name',
                                'unit',
                                'casNumber',
                                'compartment',
                                'subcompartment'], axis=1)
        self.STR = STR.sort(columns=self.STR_order)

        # Log event
        sha1 = self.__hash_file(fp)
        msg = "{} extracted from {} with SHA-1 of {}"
        self.log.info(msg.format('Elementary flows', self.__ELEXCHANGE, sha1))

    def build_PRO(self):
        """ Builds minimalistic intermediate exchange process labels

        This functions parses all files in dataset folder.  The list is
        returned as pandas DataFrame.  The index of the DataFrame is the
        filename of the files in the DATASET folder.


        Args: None
        ----

        Behaviour influenced by:
        ------------------------

        * self.PRO_order: Determines how labels are ordered

        Returns: None
        -------

        Generates: self.PRO:    DataFrame with file_Id's for index
        ----------



        """

        # INITIALIZE
        # ----------

        # Use ecospold filenames as indexes (they combine activity Id and
        # reference-product Id)
        data_folder = os.path.join(self.sys_dir, 'datasets')
        spold_files = glob.glob(os.path.join(data_folder, '*.spold'))
        _in = [os.path.splitext(os.path.basename(fn))[0] for fn in spold_files]

        # Initialize empty DataFrame
        PRO = pd.DataFrame(index=_in, columns=('activityId',
                                               'productId',
                                               'activityName',
                                               'ISIC',
                                               'EcoSpoldCategory',
                                               'geography',
                                               'technologyLevel',
                                               'macroEconomicScenario'))

        # LOOP THROUGH ALL FILES TO EXTRACT ADDITIONAL DATA
        # -------------------------------------------------

        # Log event
        if len(spold_files) > 1000:
            msg_many_files = 'Processing {} files - this may take a while ...'
            self.log.info(msg_many_files.format(len(spold_files)))

        for sfile in spold_files:

            # Remove filename extension
            file_index = os.path.splitext(os.path.basename(sfile))[0]

            # Parse xml tree
            root = ET.parse(sfile).getroot()

            try:

                # Record product Id
                PRO.ix[file_index, 'productId'] = file_index.split('_')[1]

                # Find activity dataset
                child_ds = root.find(self.__PRE + 'childActivityDataset')
                if child_ds is None:
                    child_ds = root.find(self.__PRE + 'activityDataset')
                activity_ds = child_ds.find(self.__PRE + 'activityDescription')

                # Loop through activity dataset
                for entry in activity_ds:

                    # Get name, id, etc
                    if entry.tag == self.__PRE + 'activity':
                        PRO.ix[file_index, 'activityId'] = entry.attrib['id']
                        PRO.ix[file_index, 'activityName'] = entry.find(
                                self.__PRE + 'activityName').text
                        continue

                    # Get classification codes
                    if entry.tag == self.__PRE + 'classification':
                        if 'ISIC' in entry.find(self.__PRE +
                                                'classificationSystem').text:
                            PRO.ix[file_index, 'ISIC'] = entry.find(
                                       self.__PRE + 'classificationValue').text

                        if 'EcoSpold' in entry.find(
                                     self.__PRE + 'classificationSystem').text:

                            PRO.ix[file_index, 'EcoSpoldCategory'
                                  ] = entry.find(self.__PRE +
                                                  'classificationValue').text
                        continue

                    # Get geography
                    if entry.tag == self.__PRE + 'geography':
                        PRO.ix[file_index, 'geography'
                              ] = entry.find(self.__PRE + 'shortname').text
                        continue

                    # Get Technology
                    if entry.tag == self.__PRE + 'technology':
                        PRO.ix[file_index, 'technologyLevel'
                              ] = entry.attrib['technologyLevel']
                        continue

                    # Find MacroEconomic scenario
                    if entry.tag == self.__PRE + 'macroEconomicScenario':
                        PRO.ix[file_index, 'macroEconomicScenario'
                              ] = entry.find(self.__PRE + 'name').text
                        continue
            except:
                # Log if something goes wrong
                msg_notParsed = 'File {} could not be parsed'

                # log failure
                self.log.warn(msg_notParsed.format(str(sfile)))

                # Record failure directly in PRO
                PRO.ix[file_index, 'activityId'] = str(file_index)
                PRO.ix[file_index,
                       'activityName'] = msg_notParsed.format(str(sfile))

            # quality check of id and index
            if file_index.split('_')[0] != PRO.ix[file_index, 'activityId']:
                self.log.warn('Index based on file {} and activityId in the'
                              ' xml data are different'.format(str(sfile)))

        # Final touches and save to self
        PRO['technologyLevel'] = PRO['technologyLevel'].astype(int)
        self.PRO = PRO.sort(columns=self.PRO_order)

    # =========================================================================
    # CLEAN UP FUNCTIONS: if imperfections in ecospold data
    def __find_unsourced_flows(self):
        """
        find input/use flows that do not have a specific supplying activity.

        It determines the traceable or untraceable character of a product flow
        based on the sourceActivityId field.

        Depends on:
        ----------

        *  self.inflows    (from self.get_flows or self.extract_flows)
        *  self.products   [optional] (from self.extract_products)
        *  self.PRO        [optional] (from self.get_labels or self.build_PRO)

        Generates:
        ----------

        * self.unsourced_flows: dataFrame w/ descriptions of unsourced flows

        Args: None
        ----

        Returns: None
        --------

        """
        # Define boolean vector of product flows without source
        nuns = np.equal(self.inflows['sourceActivityId'].values, None)
        unsourced_flows = self.inflows[nuns]

        # add potentially useful information from other tables
        # (not too crucial)
        try:
            unsourced_flows = self.inflows[nuns].reset_index().merge(
                    self.PRO[['activityName', 'geography', 'activityType']],
                    left_on='fileId', right_index=True)

            unsourced_flows = unsourced_flows.merge(
                    self.products[['productName', 'productId']],
                    on='productId')

            # ... and remove useless information
            unsourced_flows.drop(['sourceActivityId'], axis=1, inplace=True)
        except:
            pass

        # Log event and save to self
        if np.any(nuns):
            self.log.warn('Found {} untraceable flows'.format(np.sum(nuns)))
            self.unsourced_flows = unsourced_flows
        else:
            self.log.info('OK.   No untraceable flows.')

    def __fix_flow_sources(self):
        """ Try to find source activity for every product input-flow

        Dependencies:
        -------------

        * self.unsourced_flows  (from self.__find_unsourced_flows)
        * self.PRO              (from self.get_labels or self.build_PRO)
        * self.inflows          (from self.get_flows or self.extract_flows)

        Potentially modifies all three dependencies above to assign unambiguous
        source for each product flow, following these rules:

        1) If only one provider in dataset, pick this one, even if geography is
           wrong

        2) Else if only one producer and one market, pick the market (as the
           producer clearly sells to the sole market), regardless of geography

        3) Elseif one market in right geography, always prefer that to any
           other.

        4) Elseif only one producer with right geography, prefer that one.

        5) Otherwise, no unambiguous answer, leave for user to fix manually

        """

        # Proceed to find clear sources for these flows, if at all possible
        for i in self.unsourced_flows.index:
            aflow = self.unsourced_flows.irow(i)

            # Boolean vectors for relating the flow under investigation with
            # PRO, either in term of which industries require the product
            # in question (boPro), or the geographical location of the flow
            # (boGeo) or whether a the source activity is a market or an
            # ordinary activity (boMark), or a compbination of the above
            boPro = (self.PRO.productId == aflow.productId).values
            boGeo = (self.PRO.geography == aflow.geography).values
            boMark = (self.PRO.activityType == '1').values
            boMarkGeo = np.logical_and(boGeo, boMark)
            boProGeo = np.logical_and(boPro, boGeo)
            boProMark = np.logical_and(boPro, boMark)

            act_id = ''   # goal: finding a plausible value for this variable

            # If it is a "normal" activity that has this input flow
            if aflow.activityType == '0':

                # Maybe there are NO producers, period.
                if sum(boPro) == 0:
                    msg_noprod = "No producer found for product {}! Not good."
                    self.log.error(msg_noprod.format(aflow.productId))
                    self.log.warning("Creation of dummy producer not yet"
                                     " automated")

                # Maybe there is no choice, only ONE producer
                elif sum(boPro) == 1:

                    act_id = self.PRO[boPro].activityId.values

                    # Log event
                    if any(boProGeo):
                        # and all is fine geographically for this one producer
                        self.log.warn("Exactly 1 producer ({}) for product {}"
                                      ", and its geography is ok for this"
                                      " useflow".format(act_id,
                                                        aflow.productId))
                    else:
                        # but has wrong geography... geog proxy
                        wrong_geo = self.PRO[boPro].geography.values

                        msg = ("Exactly 1 producer ({}) for product {}, used"
                               " in spite of having wrong geography for {}:{}")
                        self.log.warn(msg.format(act_id,
                                                 aflow.productId,
                                                 aflow.fileId,
                                                 wrong_geo))

                # Or there is only ONE producer and ONE market, no choice
                # either, since then it is clear that this producer sells to
                # the market.
                elif sum(boPro) == 2 and sum(boProMark) == 1:

                    act_id = self.PRO[boProMark].activityId.values

                    # Log event
                    self.log.warn = ("Exactly 1 producer and 1 market"
                                     " worldwide, so we source product {} from"
                                     " market {}".format(aflow.productId,
                                                         act_id))

                # or there are multiple sources, but only one market with the
                # right geography
                elif sum(boMarkGeo) == 1:

                    act_id = self.PRO[boMarkGeo].activityId.values

                    # Log event
                    msg_markGeo = ('Multiple sources, but only one market ({})'
                                   ' with right geography ({}) for product {}'
                                   ' use by {}.')
                    self.log.warn(msg_markGeo.format(act_id,
                                                     aflow.geography,
                                                     aflow.productId,
                                                     aflow.fileId))

                # Or there are multiple sources, but only one producer with the
                # right geography.
                elif sum(boProGeo) == 1:

                    act_id = self.PRO[boProGeo].activityId.values

                    # Log event
                    msg_markGeo = ('Multiple sources, but only one producer'
                                   ' ({}) with right geography ({}) for'
                                   ' product {} use by {}.')
                    self.log.warn(msg_markGeo.format(act_id,
                                                     aflow.geography,
                                                     aflow.productId,
                                                     aflow.fileId))
                else:

                    # No unambiguous fix, save options to file, let user decide

                    filename = ('potentialSources_' + aflow.productId +
                                '_' + aflow.fileId + '.csv')
                    debug_file = os.path.join(self.log_dir, filename)

                    # Log event
                    msg = ("No unambiguous fix. {} potential sources for "
                           "product {} use by {}. Will need manual fix, see"
                           " {}.")
                    self.log.error(msg.format(sum(boPro),
                                              aflow.productId,
                                              aflow.fileId,
                                              debug_file))

                    self.PRO.ix[boPro, :].to_csv(debug_file, sep='|',
                                                 encoding='utf-8')

                # Based on choice of act_id, record the selected source
                # activity in inflows
                self.inflows.ix[aflow['index'], 'sourceActivityId'] = act_id

            elif aflow.activityType == '1':
                msg = ("A market with untraceable inputs:{}. This is not yet"
                       " supported by __fix_flow_sources.")
                self.log.error(msg.format(aflow.fileId))  # do something!

            else:
                msg = ("Activity {} is neither a normal one nor a market. Do"
                       " not know what to do with its" " untraceable flow of"
                       " product {}").format(aflow.fileId, aflow.productId)
                self.log.error(msg)  # do something!

    def __fix_missing_activities(self):
        """ Fix if flow sourced explicitly to an activity that does not exist
        Identifies existence of missing production, and generate them

        Depends on or Modifies:
        -----------------------

        *  self.inflows    (from self.get_flows or self.extract_flows)
        *  self.outflows   (from self.get_flows or self.extract_flows)
        *  self.products   (from self.extract_products)
        *  self.PRO        (from self.get_labels or self.build_PRO)

        Generates:
        ----------
        self.missing_activities

        """

        # Get set of all producer-product pairs in inflows
        flows = self.inflows[['sourceActivityId', 'productId']].values.tolist()
        set_flows = set([tuple(i) for i in flows])

        # Set of all producer-product pairs in PRO
        processes = self.PRO[['activityId', 'productId']].values.tolist()
        set_labels = set([tuple(i) for i in processes])

        # Identify discrepencies: missing producer-product pairs in labels
        missing_activities = set_flows - set_labels

        if missing_activities:

            # Complain
            msg = ("Found {} product flows traceable to sources that do not"
                   " produce right product. Productions will have to be added"
                   " to PRO, which now contain {} productions. Please see"
                   " missingProducers.csv.")
            self.log.error(msg.format(len(missing_activities), len(self.PRO)))

            # Organize in dataframe
            miss = pd.DataFrame([[i[0], i[1]] for i in missing_activities],
                                columns=['activityId', 'productId'],
                                index=[i[0] + '_' + i[1]
                                       for i in missing_activities])
            activity_cols = ['activityId', 'activityName', 'ISIC']
            product_cols = ['productId', 'productName']
            copied_cols = activity_cols + product_cols
            copied_cols.remove('productName')

            # Merge to get info on missing flows
            miss = miss.reset_index()
            miss = miss.merge(self.PRO[activity_cols],
                              how='left',
                              on='activityId')
            miss = miss.merge(self.products[product_cols],
                              how='left', on='productId')
            miss = miss.set_index('index')

            # Save missing flows to file for inspection
            miss.to_csv(os.path.join(self.log_dir, 'missingProducers.csv'),
                        sep='|', encodng='utf-8')

            # Insert dummy productions
            for i, row in miss.iterrows():
                self.log.warn('New dummy activity:', i)

                # add row to self.PRO
                self.PRO.ix[i, copied_cols] = row[copied_cols]
                self.PRO.ix[i, 'comment'] = 'DUMMY PRODUCTION'

                # add new row in outputflow
                self.outflows.ix[i, ['fileId', 'productId', 'amount']
                                ] = [i, row['productId'], 1.0]

            self.log.warn("Added dummy productions to PRO, which"
                          " is now {} processes long.".format(len(self.PRO)))

            self.missing_activities = missing_activities

        else:
            self.log.info("OK. Source activities seem in order. Each product"
                          " traceable to an activity that actually does"
                          " produce or distribute this product.")

    # =========================================================================
    # ASSEMBLE MATRICES: now all parsed and cleanead, build the final matrices
    def complement_labels(self):
        """ Add extra data from self.products and self.activities to labels

        Until complement_labels is run, labels are kept to the strict minimum
        to facilitate tinkering with them if needed to fix discrepancies in
        database. For example, adding missing activity or creating a dummy
        process in labels is easier without all the (optional) extra meta-data
        in there.

        Once we have a consistent symmetric system, it's time to add useful
        information to the matrix row and column labels for human readability.


        Depends on:
        -----------

            self.products       (from self.extract_products)
            self.activities     (from self.extract_activities)


        Modifies:
        ---------

            self.PRO            (from self.get_labels or self.build_PRO)
            self.STR            (from self.get_labels or self.build_STR)


        """

        self.PRO = self.PRO.reset_index()

        # add data from self.products
        self.PRO = self.PRO.merge(self.products,
                                            how='left',
                                            on='productId')

        # add data from self.activities
        self.PRO = self.PRO.merge(self.activities,
                                            how='left',
                                            on='activityId')

        # Final touches and re-establish indexes as before
        self.PRO = self.PRO.drop('unitId', axis=1).set_index('index')

        # Re-sort processes (in fix-methods altered order/inserted rows)
        self.PRO = self.PRO.sort(columns=self.PRO_order)
        self.STR = self.STR.sort(columns=self.STR_order)

    def build_AF(self):
        """
        Arranges flows as Leontief technical coefficient matrix + extensions

        Dependencies:
        -------------
        * self.inflows                (from get_flows or extract_flows)
        * self.elementary_flows       (from get_flows or extract_flows)
        * self.outflows               (from get_flows or extract_flows)
        * self.PRO [final version]    (completed by self.complement_labels)
        * self.STR [final version]    (completed by self.complement_labels)


        Behaviour determined by:
        -----------------------

        * self.positive_waste       (determines sign convention to use)
        * self.nan2null

        Generates:
        ----------

        * self.A
        * self.F

        """

        # By pivot tables, arrange all intermediate and elementary flows as
        # matrices
        z = pd.pivot(
                self.inflows['sourceActivityId'] + '_' + self.inflows['productId'],
                self.inflows['fileId'],
                self.inflows['amount']
                ).reindex(index=self.PRO.index, columns=self.PRO.index)

        g = pd.pivot_table(self.elementary_flows,
                           values='amount',
                           index='elementaryExchangeId',
                           columns='fileId').reindex(index=self.STR.index,
                                                  columns=self.PRO.index)

        # Take care of sign convention for waste
        if self.positive_waste:
            sign_changer = self.outflows['amount'] / self.outflows['amount'].abs()
            z = z.mul(sign_changer, axis=0)
            col_normalizer = 1 / self.outflows['amount'].abs()
        else:
            col_normalizer = 1 / self.outflows['amount']

        # Normalize flows
        A = z.mul(col_normalizer, axis=1)
        F = g.mul(col_normalizer, axis=1)

        if self.nan2null:
            A.fillna(0, inplace=True)
            F.fillna(0, inplace=True)

        # Reorder all rows and columns to fit labels
        self.A = A.reindex(index=self.PRO.index, columns=self.PRO.index)
        self.F = F.reindex(index=self.STR.index, columns=self.PRO.index)

    def scale_up_AF(self):
        """ Calculate absolute flow matrix from A, F, and production Volumes

        In other words, scales up normalized system description to reach
        recorded production volumes

        Dependencies:
        --------------

        * self.outflows
        * self.A
        * self.F

        Generates:
        ----------
        * self.Z
        * self.G_pro

        """
        q = self.outflows['productionVolume']

        self.Z = self.A.multiply(q, axis=1).reindex_like(self.A)
        self.G_pro = self.F.multiply(q, axis=1).reindex_like(self.F)

    def build_sut(self, make_untraceable=False):
        """ Arranges flow data as Suply and Use Tables and extensions

        Args:
        -----
        * make_untraceable: Whether or not to aggregate away the source
                            activity dimension, yielding a use table in which
                            products are no longer linked to their providers
                            [default: False; don't do it]


        Dependencies:
        -------------

        * self.inflows
        * self.outflows
        * self.elementary_flows


        Behaviour determined by:
        -----------------------
        * self.nan2null

        Generates:
        ----------

        * self.U
        * self.V
        * self.G_act
        * self.V_prodVol

        """

        def remove_productId_from_fileId(flows):
            """subfunction to remove the 'product_Id' part of 'fileId' data in
            DataFrame, leaving only activityId and renaming columnd as such"""

            fls = flows.replace('_[^_]*$', '', regex=True)
            fls.rename(columns={'fileId': 'activityId'}, inplace=True)
            return fls

        # Refocus on activity rather than process (activityId vs fileId)
        outfls = remove_productId_from_fileId(self.outflows)
        elfls = remove_productId_from_fileId(self.elementary_flows)
        infls = remove_productId_from_fileId(self.inflows)
        infls.replace(to_replace=[None], value='', inplace=True)

        # Pivot flows into Use and Supply and extension tables
        self.U = pd.pivot_table(infls,
                                index=['sourceActivityId', 'productId'],
                                columns='activityId',
                                values='amount',
                                aggfunc=np.sum)

        self.V = pd.pivot_table(outfls,
                                index='productId',
                                columns='activityId',
                                values='amount',
                                aggfunc=np.sum)

        self.V_prodVol = pd.pivot_table(outfls,
                                        index='productId',
                                        columns='activityId',
                                        values='productionVolume',
                                        aggfunc=np.sum)

        self.G_act = pd.pivot_table(elfls,
                                    index='elementaryExchangeId',
                                    columns='activityId',
                                    values='amount',
                                    aggfunc=np.sum)

        # ensure all products are covered in supply table
        self.V = self.V.reindex(index=self.products.index,
                                columns=self.activities.index)
        self.V_prodVol = self.V_prodVol.reindex(index=self.products.index,
                                                columns=self.activities.index)
        self.U = self.U.reindex(columns=self.activities.index)
        self.G_act = self.G_act.reindex(index=self.STR.index,
                                        columns=self.activities.index)

        if make_untraceable:
            # Optionally aggregate away sourceActivity dimension, more IO-like
            # Supply and untraceable-Use tables...
            self.U = self.U.groupby(level='productId').sum()
            self.U = self.U.reindex(index=self.products.index,
                                    columns=self.activities.index)
            self.log.info("Aggregated all sources in U, made untraceable")

        if self.nan2null:
            self.U.fillna(0, inplace=True)
            self.V.fillna(0, inplace=True)
            self.G_act.fillna(0, inplace=True)

    # =========================================================================
    # SANITY CHECK: Compare calculated cummulative LCI with official values
    def build_E(self, data_folder=None):
        """ Extract matrix of cummulative LCI from ecospold files

        Dependencies:
        ------------
        * self.PRO
        * self.STR

        Behaviour influenced by:
        ------------------------

        * self.lci_dir
        * self.nan2null

        Generates:
        ----------

        * self.E

        """

        # Get list of ecospold files
        if data_folder is None:
            data_folder = self.lci_dir
        spold_files = glob.glob(os.path.join(data_folder, '*.spold'))
        msg = "Processing {} {} files from {}"
        self.log.info(msg.format(len(spold_files),
                                 'cummulative LCI',
                                 data_folder))

        # Initialize (huge) dataframe and get dimensions
        self.log.info('creating E dataframe')
        self.E = pd.DataFrame(index=self.STR.index,
                                columns=self.PRO.index, dtype=float)
        initial_rows, initial_columns = self.E.shape

        # LOOP OVER ALL FILES TO EXTRACT ELEMENTARY FLOWS
        for count, sfile in enumerate(spold_files):

            # Get to flow data
            current_file = os.path.basename(sfile)
            current_id = os.path.splitext(current_file)[0]
            root = ET.parse(sfile).getroot()
            child_ds = root.find(self.__PRE + 'childActivityDataset')
            if child_ds is None:
                child_ds = root.find(self.__PRE + 'activityDataset')
            flow_ds = child_ds.find(self.__PRE + 'flowData')

            # Find elemementary exchanges amongst all flows
            for entry in flow_ds:
                if entry.tag == self.__PRE + 'elementaryExchange':
                    try:
                        # Get amount
                        self.E.ix[entry.attrib['elementaryExchangeId'],
                                    current_id] = float(entry.attrib['amount'])
                    except:
                        _amount = entry.attrib.get('amount', 'not found')
                        if _amount != '0':
                            msg = ("Parser warning: elementary exchange in {0}"
                                   ". elementaryExchangeId: {1} - amount: {2}")
                            self.log.warn(msg.format(str(current_file),
                                    entry.attrib.get('elementaryExchangeId',
                                                     'not found'), _amount))

            # keep user posted, as this loop can be quite long
            if count % 300 == 0:
                self.log.info('Completed {} files.'.format(count))

        # Check for discrepancies in list of stressors and processes
        final_rows, final_columns = self.E.shape
        appended_rows = final_rows - initial_rows
        appended_columns = final_columns - initial_columns

        # and log
        if appended_rows != 0:
            self.log.warn('There are {} new processes relative to the initial'
                          'list.'.format(str(appended_rows)))
        if appended_columns != 0:
            self.log.warn('There are {} new impacts relative to the initial'
                          'list.'.format(str(appended_rows)))

        if self.nan2null:
            self.E.fillna(0, inplace=True)

    def __calculate_E(self, A0, F0):
        """ Calculate lifecycle cummulative inventories for comparison self.E

        Args:
        -----
        * A0 : Leontief A-matrix (pandas dataframe)
        * F0 : Environmental extension (pandas dataframe)

        Returns:
        --------
        * Ec as pandas dataframe

        Note:
        --------
        * Plan to move this as nested function of cummulative_lci_check

        """
        A = A0.fillna(0).values
        F = F0.fillna(0).values
        I = np.eye(len(A))
        Ec = F.dot(np.linalg.solve(I - A, I))
        return pd.DataFrame(Ec, index=F0.index, columns=A0.columns)


    def cummulative_lci_check(self, rtol=1e-2, atol=1e-5, imax=3):
        """
        Sanity check: compares calculated and parsed cummulative LCI data

        Args:
        -----

        * rtol: relative tolerance, maximum relative difference  between
                coefficients
        * atol: absolute tolerance, maximum absolute difference  between
                coefficients

        * imax: Number of orders of magnitude smaller than defined tolerance
                that should be investigated


        Depends on:
        ----------
        * self.E
        * self.__calculate_E()

        """

        Ec = self.__calculate_E(self.A, self.F)

        filename = os.path.join(self.log_dir,
                                'qualityCheckCummulativeLCI.shelf')
        shelf = shelve.open(filename)

        # Perform compareE() analysis at different tolerances, in steps of 10
        i = 1
        while (i <= imax) and (rtol > self.rtolmin):
            bad = self.compareE(Ec, rtol, atol)
            rtol /= 10
            if bad is not None:
                # Save bad flows in Shelf persistent dictionary
                shelf['bad_at_rtol'+'{:1.0e}'.format(rtol)] = bad
                i += 1
        shelf.close()
        sha1 = self.__hash_file(filename)
        msg = "{} saved in {} with SHA-1 of {}"
        self.log.info(msg.format('Cummulative LCI differences',
                                 filename,
                                 sha1))

    def compareE(self, Ec, rtol=1e-2, atol=1e-5):
        """ Compare parsed (official) cummulative lci emissions (self.E) with
        lifecycle emissions calculated from the constructed matrices (Ec)
        """

        thebad = None

        # Compare the two matrices, see how many values are "close"
        close = np.isclose(abs(self.E), abs(Ec), rtol, atol, equal_nan=True)
        notclose = np.sum(~ close)
        self.log.info('There are {} lifecycle cummulative emissions that '
                      'differ by more than {} % AND by more than {} units '
                      'relative to the official value.'.format(notclose,
                                                               rtol*100,
                                                               atol))

        if notclose:
            # Compile a Series of all "not-close" values
            thebad = pd.concat([self.E.mask(close).stack(), Ec.mask(close).stack()], 1)
            thebad.columns = ['official', 'calculated']
            thebad.index.names = ['stressId', 'fileId']

            # Merge labels to make it human readable
            thebad = pd.merge(thebad.reset_index(),
                              self.PRO[['productName', 'activityName']],
                              left_on='fileId',
                              right_on=self.PRO.index)

            thebad = pd.merge(thebad,
                              self.STR,
                              left_on='stressId',
                              right_on=self.STR.index).set_index(['stressId',
                                                                  'fileId'])

        return thebad

    # =========================================================================
    # EXPORT AND HELPER FUNCTIONS
    def save_system(self, file_formats=None):
        """ Save normalized syste matrices to different formats

        Args:
        -----
        * fileformats : List of file formats in which to save data
                        [Default: None, save to all possible file formats]

                         Options: 'Pandas'       --> pandas dataframes
                                  'SparsePandas' --> sparse pandas dataframes
                                  'SparseMatrix' --> scipy AND matlab sparse
                                  'csv'          --> text with separator =  '|'

        This method creates separate files for normalized, symmetric matrices
        (A, F), scaled-up symmetric metrices (Z, G_pro), and supply and use
        data (U, V, V_prod, G_act).

        For Pandas and sparse pickled files, ghis method organizes the
        variables in a dictionary, and pickles this dictionary to file.

            For sparse pickled file, some labels are not turned into sparse
            matrices (because not sparse) and are rather added as numpy arrays.

        For Matlab sparse matrices, variables saved to mat file.

        For CSV, a subdirectory is created to hold one text file per variable.


        Returns:
        -------
            None

        """

        def pickling(filename, adict, what_it_is, mat):
            """ subfunction that handles creation of binary files """

            # save dictionary as pickle
            with open(filename + '.pickle', 'wb') as fout:
                pickle.dump(adict, fout)
            sha1 = self.__hash_file(filename + '.pickle')
            msg = "{} saved in {} with SHA-1 of {}"
            self.log.info(msg.format(what_it_is, filename + '.pickle', sha1))

            # save dictionary also as mat file
            if mat:
                scipy.io.savemat(filename, adict, do_compression=True)
                sha1 = self.__hash_file(filename + '.mat')
                msg = "{} saved in {} with SHA-1 of {}"
                self.log.info(msg.format(what_it_is, filename + '.mat', sha1))

        def pickle_symm_norm(PRO, STR, A, F, mat=False):
            """ nested function that prepares dictionary for symmetric,
            normalized (coefficient) system description file """

            adict = {'PRO': PRO,
                     'STR': STR,
                     'A': A,
                     'F': F}
            pickling(file_pr + '_symmNorm', adict,
                     'Final, symmetric, normalized matrices', mat)

        def pickle_symm_scaled(PRO, STR, Z, G_pro, mat=False):
            """ nested function that prepares dictionary for symmetric,
            scaled (flow) system description file """

            adict = {'PRO': PRO,
                     'STR': STR,
                     'Z': Z,
                     'G_pro': G_pro}
            pickling(file_pr + '_symmScale', adict,
                     'Final, symmetric, scaled-up flow matrices', mat)

        def pickle_sut(prod, act, STR, U, V, V_prodVol, G_act, mat=False):
            """ nested function that prepares dictionary for SUT file """

            adict = {'products': prod,
                     'activities': act,
                     'STR': STR,
                     'U': U,
                     'V': V,
                     'V_prodVol': V_prodVol,
                     'G_act': G_act}

            pickling(file_pr + '_SUT', adict, 'Final SUT matrices', mat)

        # save as full Dataframes
        format_name = 'Pandas'
        if file_formats is None or format_name in file_formats:

            file_pr = os.path.join(self.out_dir,
                                   self.project_name + format_name)
            if self.A is not None:
                pickle_symm_norm(self.PRO, self.STR, self.A, self.F)
            if self.Z is not None:
                pickle_symm_scaled(self.PRO, self.STR, self.Z, self.G_pro)
            if self.U is not None:
                pickle_sut(self.products,
                           self.activities,
                           self.STR,
                           self.U, self.V, self.V_prodVol, self.G_act)

        # save sparse Dataframes
        format_name = 'SparsePandas'
        if file_formats is None or format_name in file_formats:

            file_pr = os.path.join(self.out_dir,
                                   self.project_name + format_name)
            if self.A is not None:
                A = self.A.to_sparse()
                F = self.F.to_sparse()
                pickle_symm_norm(self.PRO, self.STR, A, F)
            if self.Z is not None:
                Z = self.Z.to_sparse()
                G_pro = self.G_pro.to_sparse()
                pickle_symm_scaled(self.PRO, self.STR, Z, G_pro)
            if self.U is not None:
                U = self.U.to_sparse()
                V = self.V.to_sparse()
                V_prodVol = self.V_prodVol.to_sparse()
                G_act = self.G_act.to_sparse()
                pickle_sut(self.products,
                           self.activities,
                           self.STR,
                           U, V, V_prodVol, G_act)

        # save as sparse Matrices (both pickled and mat-files)
        format_name = 'SparseMatrix'
        if file_formats is None or format_name in file_formats:

            file_pr = os.path.join(self.out_dir,
                                   self.project_name + format_name)
            PRO = self.PRO.reset_index().fillna('').values
            STR = self.STR.reset_index().fillna('').values
            if self.A is not None:
                A = scipy.sparse.csc_matrix(self.A.fillna(0))
                F = scipy.sparse.csc_matrix(self.F.fillna(0))
                pickle_symm_norm(PRO, STR, A, F, mat=True)
            if self.Z is not None:
                Z = scipy.sparse.csc_matrix(self.Z.fillna(0))
                G_pro = scipy.sparse.csc_matrix(self.G_pro.fillna(0))
                pickle_symm_scaled(PRO, STR, Z, G_pro, mat=True)
            if self.U is not None:
                U = scipy.sparse.csc_matrix(self.U.fillna(0))
                V = scipy.sparse.csc_matrix(self.V.fillna(0))
                V_prodVol = scipy.sparse.csc_matrix(self.V_prodVol.fillna(0))
                G_act = scipy.sparse.csc_matrix(self.G_act.fillna(0))
                products = self.products.values  # to numpy array, not sparse
                activities = self.activities.values
                pickle_sut(products,
                           activities,
                           STR,
                           U, V, V_prodVol, G_act, mat=True)

        # Write to CSV files
        format_name = 'csv'
        if file_formats is None or format_name in file_formats:

            csv_dir = os.path.join(self.out_dir, 'csv')
            if not os.path.exists(csv_dir):
                os.makedirs(csv_dir)
            self.PRO.to_csv(os.path.join(csv_dir, 'PRO.csv'))
            self.STR.to_csv(os.path.join(csv_dir, 'STR.csv'))
            if self.A is not None:
                self.A.to_csv(os.path.join(csv_dir, 'A.csv'), sep='|')
                self.F.to_csv(os.path.join(csv_dir, 'F.csv'), sep='|')
                self.log.info("Final matrices saved as CSV files")
            if self.Z is not None:
                self.Z.to_csv(os.path.join(csv_dir, 'Z.csv'), sep='|')
                self.G_pro.to_csv(os.path.join(csv_dir, 'G_pro.csv'), sep='|')
            if self.U is not None:
                self.products.to_csv(os.path.join(csv_dir, 'products.csv'),
                                     sep='|')
                self.activities.to_csv(os.path.join(csv_dir, 'activities.csv'),
                                       sep='|')
                self.U.to_csv(os.path.join(csv_dir, 'U.csv'), sep='|')
                self.V.to_csv(os.path.join(csv_dir, 'V.csv'), sep='|')
                self.V_prodVol.to_csv(os.path.join(csv_dir, 'V_prodVol.csv'),
                                      sep='|')
                self.G_act.to_csv(os.path.join(csv_dir, 'G_act.csv'), sep='|')

    def __hash_file(self, afile):
        """ Get SHA-1 hash of binary file

        Args:
        -----
        * afile: either name of file or file-handle of a file opened in
                 "read-binary" ('rb') mode.

        Returns:
        --------
        * hexdigest hash of file, as string

        """

        blocksize = 65536
        hasher = hashlib.sha1()
        # Sometimes used for afile as filename
        try:
            f = open(afile, 'rb')
            opened_here = True
        # Or afile can be a filehandle
        except:
            f = afile
            opened_here = False

        buf = f.read(blocksize)
        while len(buf) > 0:
            hasher.update(buf)
            buf = f.read(blocksize)

        if opened_here:
            f.close()

        return hasher.hexdigest()

    def __deduplicate(self, raw_list, idcol=None, name=''):
        """ Removes duplicate entries from a list

        and then optionally warns of duplicate id's in the cleaned up list

        Args:
        -----
            raw_list: a list
            incol : in case of a list of lists, the "column" position for id's
            name: string, just for logging messages

        Return:
        ------
           deduplicated: list without redundant entries
           duplicates: list with redundant entries
           id_deduplicated: list of ID's without redundancy
           id_duplicates: list of redundant ID's

        """

        # Initialization
        deduplicated = []
        duplicates = []
        id_deduplicated = []
        id_duplicates = []

        # Find duplicate rows in list
        for elem in raw_list:
            if elem not in deduplicated:
                deduplicated.append(elem)
            else:
                duplicates.append(elem)

        # If an "index column" is specified, find duplicate indexes in
        # deduplicated. In other words, amongst unique rows, are there
        # non-unique IDs?
        if idcol is not None:
            indexes = [row[idcol] for row in deduplicated]
            for index in indexes:
                if index not in id_deduplicated:
                    id_deduplicated.append(index)
                else:
                    id_duplicates.append(index)

        # Log findings for further inspection
        if duplicates:
            filename = 'duplicate_' + name + '.csv'
            with open(os.path.join(self.log_dir, filename), 'w') as fout:
                duplicatewriter = csv.writer(fout, delimiter='|')
                duplicatewriter.writerows(duplicates)
            msg = 'Removed {} duplicate rows from {}, see {}.'
            self.log.warn(msg.format(len(duplicates), name, filename))

        if id_duplicates:
            filename = 'duplicateID_' + name + '.csv'
            with open(os.path.join(self.log_dir + filename), 'w') as fout:
                duplicatewriter = csv.writer(fout, delimiter='|')
                duplicatewriter.writerows(id_duplicates)
            msg = 'There are {} duplicate Id in {}, see {}.'
            self.log.warn(msg.format(len(id_duplicates), name, filename))

        return deduplicated, duplicates, id_deduplicated, id_duplicates