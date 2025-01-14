#!/usr/bin/python

"""
Definition of the wflow_w3 model.
---------------------------------------
The model is modified from the Australian Water Resources Assessment Landscape (AWRA-L) model version 0.5
W3RA is documented in van Dijk et al. (2013), Water Resour. Res., 49, 2729-2746, doi:10.1002/wrcr.20251
URL: http://onlinelibrary.wiley.com/doi/10.1002/wrcr.20251/abstract
More comprehensive documentation of AWRA-L version 0.5 can be found in:
Van Dijk, A.I.J.M. (2010) The Australian water resources assessment system
(version 0.5), 3.0.5.Technical description of the landscape hydrology model
(AWRA-L). WIRADA Technical Report, CSIRO Water for a Healthy Country
Flagship, Canberra.
URL: http://www.clw.csiro.au/publications/waterforahealthycountry/2010/wfhc-aus-water-resources-assessment-system.pdf
The section references below refer to the sections in the AWRA-L report.
Changes compared to that code are indicated, e.g. by commenting out
redundant code.
Further question please contact albert.vandijk@anu.edu.au
Port to Python/PCRaster: Deltares
Usage:
wflow_w3  -C case -R Runid -c inifile
    -C: set the name  of the case (directory) to run
    
    -R: set the name runId within the current case
    
    -c name of the config file (in the case directory)
    
$Author: schelle $
$Id: wflow_sceleton.py 898 2014-01-09 14:47:06Z schelle $
$Rev: 898 $
"""

import numpy
import os
import os.path
import shutil, glob
import getopt
import math

import xarray
import numpy as np

from wflow.wf_DynamicFramework import *
from wflow.wflow_adapt import *

import pcraster.framework
import pcraster as pcr


def usage(*args):
    sys.stdout = sys.stderr
    for msg in args:
        print(msg)
    print(__doc__)
    sys.exit(0)


def pcr_tanh(x):
    """
    define tanh for pcraster objects

    """
    return (pcr.exp(x) - pcr.exp(-x)) / (pcr.exp(x) + pcr.exp(-x))


def interp_hand(z, hand, hand_perc):

    z_lim = xarray.ufuncs.minimum(
        xarray.ufuncs.maximum(z, hand[0]), hand[hand_perc.size - 1]
    )  # limit values within measured elevation range

    iLower = hand.where(hand <= z_lim)  # find next lower elevation
    PercLower = (
        (iLower * 0 + 1.0).where(iLower == iLower.max(axis=0)) * hand_perc
    ).max(axis=0, skipna=True)
    zLower = iLower.where(iLower == iLower.max(axis=0)).max(axis=0, skipna=True)

    iUpper = hand.where(hand >= z_lim)  # find next higher elevation
    PercUpper = (
        (iUpper * 0 + 1.0).where(iUpper == iUpper.min(axis=0)) * hand_perc
    ).max(axis=0, skipna=True)
    zUpper = iUpper.where(iUpper == iUpper.min(axis=0)).max(axis=0, skipna=True)

    flim = PercLower + (PercUpper - PercLower) * xarray.ufuncs.fmax(
        0, xarray.ufuncs.fmin(1, (z_lim - zLower) / (zUpper - zLower))
    )

    pcr_flim = pcr.numpy2pcr(pcr.Scalar, flim.fillna(-999.0).values, -999.0)

    return pcr_flim


class WflowModel(pcraster.framework.DynamicModel):
    """
    The user defined model class. T
    """

    def __init__(self, cloneMap, Dir, RunDir, configfile):
        """
        *Required*

        The init function **must** contain what is shown below. Other functionality
        may be added by you if needed.

        """
        pcraster.framework.DynamicModel.__init__(self)
        self.caseName = os.path.abspath(Dir)
        self.clonemappath = os.path.join(os.path.abspath(Dir), "staticmaps", cloneMap)
        pcr.setclone(self.clonemappath)
        self.runId = RunDir
        self.Dir = os.path.abspath(Dir)
        self.configfile = configfile
        self.SaveDir = os.path.join(self.Dir, self.runId)

    def stateVariables(self):
        """
        *Required*

        Returns a list of state variables that are essential to the model.
        This list is essential for the resume and suspend functions to work.

        This function is specific for each model and **must** be present. This is
        where you specify the state variables of you model. If your model is stateless
        this function must return and empty array (states = [])
        """

        states = [
            "S0",
            "Ss",
            "Sd",
            "Mleaf",
            "FreeWater",
            "DrySnow",
            "Sg",
            "Sr",
        ]  # ,'OpenWaterFrac']

        return states

    def suspend(self):
        """
        *Required*

        Suspends the model to disk. All variables needed to restart the model
        are saved to disk as pcraster maps. Use resume() to re-read them

        This function is required.

        """

        self.logger.info("Saving initial conditions...")
        #: It is advised to use the wf_suspend() function
        #: here which will suspend the variables that are given by stateVariables
        #: function.
        self.wf_suspend(self.SaveDir + "/outstate/")

        if self.OverWriteInit:
            self.logger.info("Saving initial conditions over start conditions...")
            self.wf_suspend(self.Dir + "/instate/")

    def initial(self):

        """
        *Required*

        Initial part of the model, executed only once. It reads all static model
        information (parameters) and sets-up the variables used in modelling.

        This function is required. The contents is free. However, in order to
        easily connect to other models it is advised to adhere to the directory
        structure used in the other models.

        """
        #: pcraster option to calculate with units or cells. Not really an issue
        #: in this model but always good to keep in mind.
        pcr.setglobaloption("unittrue")
        pcr.setglobaloption(
            "radians"
        )  # Needed as W3RA was originally written in matlab

        self.timestepsecs = int(
            configget(self.config, "model", "timestepsecs", "86400")
        )

        self.reinit = int(configget(self.config, "run", "reinit", "0"))
        self.OverWriteInit = int(configget(self.config, "model", "OverWriteInit", "0"))

        self.UseETPdata = int(
            configget(self.config, "model", "UseETPdata", "1")
        )  #  1: Use ETP data, 0: Compute ETP from meteorological variables
        self.logger.debug("use DATA: " + str(self.UseETPdata))
        self.basetimestep = 86400
        self.SaveMapDir = self.Dir + "/" + self.runId + "/outmaps"

        # Input base map
        wflow_subcatch = configget(
            self.config, "model", "wflow_subcatch", "staticmaps/wflow_subcatch.map"
        )
        self.subcatch = pcr.ordinal(
            self.wf_readmap(os.path.join(self.Dir, wflow_subcatch), 0.0, fail=True)
        )  # Determines the area of calculations (all cells > 0)
        self.subcatch = pcr.ifthen(self.subcatch > 0, self.subcatch)

        self.Altitude = pcr.readmap(self.Dir + "/staticmaps/wflow_clone")

        self.latitude = pcr.ycoordinate(pcr.boolean(self.Altitude))

        # Reading of parameters extracted from global maps
        # TODO: NetIceFlow and PermIce supplied but not used in the code
        self.Fhru = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/fHRU.map"), 0.0, fail=True
        )
        self.T_offset = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/T_offset.map"), 0.0, fail=True
        )
        self.OpenWaterFrac = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/OpenWaterFrac.map"), 0.0, fail=True
        )
        self.slope = (
            self.wf_readmap(
                os.path.join(self.Dir, "staticmaps/slope.map"), 0.0, fail=True
            )
            / 100.0
        )
        self.hveg = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/hveg.map"), 0.0, fail=True
        )
        self.Gs_scalar = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Gs_scalar.map"), 0.0, fail=True
        )
        self.ER_coeff = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/ER_coeff.map"), 0.0, fail=True
        )
        self.FsoilEmax = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/FsoilEmax.map"), 0.0, fail=True
        )
        self.K0_scalar = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/K0_scalar.map"), 0.0, fail=True
        )
        self.Ksat_exp = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Ksat_exp.map"), 0.0, fail=True
        )
        self.k_s = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/k_s.map"), 0.0, fail=True
        )
        self.Lambda = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/lambda.map"), 0.0, fail=True
        )
        self.S_sls = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/S_sls.map"), 0.0, fail=True
        )
        self.snow_Cfmax = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/snow_Cfmax.map"), 0.0, fail=True
        )
        self.snow_Cfr = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/snow_CFR.map"), 0.0, fail=True
        )
        self.snow_TT = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/snow_TT.map"), 0.0, fail=True
        )
        self.snow_WHC = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/snow_WHC.map"), 0.0, fail=True
        )
        self.fImp = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/fImp.map"), 0.0, fail=True
        )
        self.Pref = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Pref.map"), 0.0, fail=True
        )
        self.psi_s = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_s.map"), 0.0, fail=True
        )
        self.fPotDeep = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/fPotDeep.map"), 0.0, fail=True
        )
        self.porosity = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/porosity.map"), 0.0, fail=True
        )
        self.K_gw = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/K_gw.map"), 0.0, fail=True
        )
        self.theta_s = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/theta_s.map"), 0.0, fail=True
        )
        self.LAImax = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/LAImax.map"), 8.0, fail=True
        )

        # Default parameters maps (supposed to be tbl?)
        self.alb_water = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/alb_water.map"), 0.05, fail=False
        )
        self.Cg = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Cg.map"), 1.940, fail=False
        )
        self.d0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/d0.map"), 0.15, fail=False
        )
        self.ds = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/ds.map"), 0.85, fail=False
        )
        self.dd = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/dd.map"), 4.00, fail=False
        )
        self.D50 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/D50.map"), 700, fail=False
        )
        self.ER_exp = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/ER_exp.map"), 0.114, fail=False
        )
        self.Fgw_conn = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Fgw_conn.map"), 1, fail=False
        )
        self.fvegref_G = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/fvegref_G.map"), 0.15, fail=False
        )
        self.FwaterE = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/FwaterE.map"), 1, fail=False
        )
        self.Gfrac_max = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Gfrac_max.map"), 0.15, fail=False
        )
        self.InitLoss = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/InitLoss.map"), 0, fail=False
        )
        self.K_rout = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/K_rout.map"), 0.5, fail=False
        )
        self.Kr_coeff = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Kr_coeff.map"), 0.0741, fail=False
        )
        self.LAIref = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/LAIref.map"), 2.4, fail=False
        )
        self.LUEmax = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/LUEmax.map"), 0.0544, fail=False
        )
        self.Pref_imp = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Pref_imp.map"), 10, fail=False
        )
        self.R0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/R0.map"), 0.789, fail=False
        )
        self.SLA = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/SLA.map"), 5, fail=False
        )
        self.slope_coeff = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/slope_coeff.map"), 0.9518, fail=False
        )
        self.snow_TTI = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/snow_TTI.map"), 1, fail=False
        )
        self.T24_snow = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/T24_snow.map"), 18, fail=False
        )
        self.Tmin = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Tmin.map"), -10, fail=False
        )
        self.Topt = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Topt.map"), 10, fail=False
        )
        self.Tgrow = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Tgrow.map"), 200, fail=False
        )
        self.Tsenc = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Tsenc.map"), 20, fail=False
        )
        self.Ud0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Ud0.map"), 6, fail=False
        )
        self.Ug0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Ug0.map"), 1, fail=False
        )
        self.Us0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/Us0.map"), 6, fail=False
        )
        self.w0ref_alb = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/w0ref_alb.map"), 0.3, fail=False
        )

        ds_hand = xarray.open_dataset(os.path.join(self.Dir, "staticmaps/HAND.nc"))

        hand = ds_hand["HAND"]
        self.HAND = xarray.concat([(hand[0] * 0.0).expand_dims("z"), hand], dim="z")

        perc_HAND = ds_hand["percentile"]
        self.perc_HAND = xarray.concat(
            [(perc_HAND[0] * 0.0).expand_dims("z"), perc_HAND], dim="z"
        )

        psi_FC = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_FC.map"), -3.3, fail=False
        )  # m or hPa or 33 kPa
        psi_FC0 = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_FC0.map"), -0.5, fail=False
        )  # m or hPa or 5 kPa - rapidly drainable theta for top soil
        psi_ERRP = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_ERRP.map"), -10, fail=False
        )  # m or 100 kPa - assumed pressure at which soil moisture starts to limit soil evaporation (following D. Tran, 2015)
        psi_d = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_d.map"), -50, fail=False
        )  # m assumed pressure at which soil moisture starts to limit soil water uptake
        psi_PWP = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_PWP.map"), -150, fail=False
        )  # m
        psi_res = self.wf_readmap(
            os.path.join(self.Dir, "staticmaps/psi_res.map"), -1e6, fail=False
        )  # m

        # Set static inital variables
        self.wf_updateparameters()
        self.wf_multparameters()

        theta_FC = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_FC)) ** self.Lambda
        )  # fraction
        theta_FC0 = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_FC0)) ** self.Lambda
        )  # fraction
        theta_ERRP = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_ERRP)) ** self.Lambda
        )  # fraction
        theta_d = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_d)) ** self.Lambda
        )  # fraction
        theta_PWP = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_PWP)) ** self.Lambda
        )  # fraction
        theta_res = (
            self.theta_s * pcr.min(1, (self.psi_s / psi_res)) ** self.Lambda
        )  # fraction

        self.S0max = (
            self.d0 * 1000 * (theta_FC0 - theta_res)
        )  # mm available storage for evaporation, note FC0 is used rather than theta_sat
        self.Ssmax = self.ds * 1000 * (self.theta_s - theta_PWP)
        self.Sdmax = self.dd * 1000 * (self.theta_s - theta_PWP)
        self.K0sat = (
            self.K0_scalar * self.k_s
        )  # mm/d - note that this is technically in fact not Ksat but K(theta_FC0)
        self.Kssat = (
            self.K0_scalar
            * (((self.ds + self.d0) / self.d0) ** -self.Ksat_exp)
            * self.k_s
        )
        self.Kdsat = (
            self.K0_scalar
            * (((self.dd + self.ds + self.d0) / self.d0) ** -self.Ksat_exp)
            * self.k_s
        )
        self.w0limE = (theta_ERRP - theta_res) / (self.theta_s - theta_res)
        self.wslimU = (theta_d - theta_PWP) / (self.theta_s - theta_PWP)
        self.wdlimU = (theta_d - theta_PWP) / (self.theta_s - theta_PWP)

        self.wf_multparameters()

        # Static, for the computation of Aerodynamic conductance (3.7)
        # self.fh = ln(813./max(self.hveg,0.25)-5.45)
        # self.ku1 = 0.305/(self.fh*(self.fh+2.3))

        self.logger.info("Starting Dynamic run...")

    def resume(self):
        """
        *Required*
        This function is required. Read initial state maps (they are output of a
        previous call to suspend()). The implementation shown here is the most basic
        setup needed.

        """
        if self.reinit == 1:
            self.logger.info("Setting initial conditions to default")

            self.Sg = pcr.cover(0.0)
            self.Sr = pcr.cover(0.0)
            self.Mleaf = 2.0 / self.SLA
            self.S0 = 0.2 * self.w0limE * self.S0max
            self.Ss = 0.2 * self.wslimU * self.Ssmax
            self.Sd = 0.2 * self.wdlimU * self.Sdmax
            self.FreeWater = pcr.cover(0.0)
            self.DrySnow = pcr.cover(0.0)

        else:
            self.logger.info("Setting initial conditions from state files")
            self.wf_resume(os.path.join(self.Dir, "instate"))

            # for s in self.stateVariables():
            #    exec "self." + s + " = cover(0)"

    def default_summarymaps(self):
        """
        *Optional*
        Return a default list of variables to report as summary maps in the outsum dir.
        """
        return ["self.SLA"]

    def parameters(self):
        """
        Define all model parameters here that the framework should handle for the model
        See wf_updateparameters and the parameters section of the ini file
        If you use this make sure to all wf_updateparameters at the start of the dynamic section
        and at the start/end of the initial section
        :returns modelparameters: list of model parameters
        """
        modelparameters = []

        # Define here the W3RA mapstacks (best to read these via netcdf)
        self.TMAX_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "TMAX", "/inmaps/TMAX"
        )
        self.TMIN_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "TMIN", "/inmaps/TMIN"
        )
        self.TDAY_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "TDAY", "/inmaps/TDAY"
        )
        self.EPOT_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "EPOT", "/inmaps/EPOT"
        )
        self.PRECIP_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "PRECIP", "/inmaps/PRECIP"
        )
        self.RAD_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "RAD", "/inmaps/RAD"
        )
        self.ALBEDO_mapstack = self.Dir + configget(
            self.config,
            "inputmapstacks",
            "ALBEDO",
            "/inmaps/ALBEDO",
        )
        self.WINDSPEED_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "WINDSPEED", "/inmaps/WIND"
        )
        self.AIRPRESS_mapstack = self.Dir + configget(
            self.config, "inputmapstacks", "AIRPRESS", "/inmaps/PRES"
        )

        # Add the timeseries to model parameters
        modelparameters.append(
            self.ParamType(
                name="TMAX",
                stack=self.TMAX_mapstack,
                type="timeseries",
                default=10.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="TMIN",
                stack=self.TMIN_mapstack,
                type="timeseries",
                default=10.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="TDAY",
                stack=self.TDAY_mapstack,
                type="timeseries",
                default=10.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="EPOT",
                stack=self.EPOT_mapstack,
                type="timeseries",
                default=0.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="PRECIP",
                stack=self.PRECIP_mapstack,
                type="timeseries",
                default=0.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="RAD",
                stack=self.RAD_mapstack,
                type="timeseries",
                default=10.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="ALBEDO",
                stack=self.ALBEDO_mapstack,
                type="timeseries",
                default=0.1,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="WINDSPEED",
                stack=self.WINDSPEED_mapstack,
                type="timeseries",
                default=1.0,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="AIRPRESS",
                stack=self.AIRPRESS_mapstack,
                type="timeseries",
                default=980.0,
                verbose=True,
                lookupmaps=[],
            )
        )

        # Add the climatologies extracted from global maps to model parameters
        # TODO: fveg, fwater, LAI, PgainF supplied but not used in the code
        modelparameters.append(
            self.ParamType(
                name="alb_dry",
                stack=(self.Dir + "/staticmaps/clim/alb_dry"),
                type="monthlyclim",
                default=0.20,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="alb_wet",
                stack=(self.Dir + "/staticmaps/clim/alb_wet"),
                type="monthlyclim",
                default=0.15,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="alb_snow",
                stack=(self.Dir + "/staticmaps/clim/alb_snow"),
                type="monthlyclim",
                default=0.60,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="cGsmax",
                stack=(self.Dir + "/staticmaps/clim/cGsmax"),
                type="monthlyclim",
                default=0.020,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="f_alb_Vc",
                stack=(self.Dir + "/staticmaps/clim/f_alb_Vc"),
                type="monthlyclim",
                default=0.4,
                verbose=True,
                lookupmaps=[],
            )
        )
        modelparameters.append(
            self.ParamType(
                name="Vc",
                stack=(self.Dir + "/staticmaps/clim/Vc"),
                type="monthlyclim",
                default=0.5,
                verbose=True,
                lookupmaps=[],
            )
        )

        return modelparameters

    def dynamic(self):
        """
        *Required*
        This is where all the time dependent functions are executed. Time dependent
        output should also be saved here.
        """
        # print 'useETPdata' , self.UseETPdata
        # Put the W3RA here. Stuff from W3RA_timestep_model.m
        # read meteo from file
        self.logger.debug("Running for: " + str(self.currentdatetime))
        self.wf_updateparameters()
        #        self.PRECIP = cover(
        #            self.wf_readmap(self.PRECIP_mapstack, 0.0), scalar(0.0)
        #        )  # mm
        #
        #        if self.UseETPdata == 1:
        #            self.TDAY = cover(
        #                self.wf_readmap(self.TDAY_mapstack, 10.0), scalar(10.0)
        #            )  # T in degC
        #            self.EPOT = cover(
        #                self.wf_readmap(self.EPOT_mapstack, 0.0), scalar(0.0)
        #            )  # mm
        #            self.WINDSPEED = cover(
        #                self.wf_readmap(self.WINDSPEED_mapstack, default=1.0), scalar(1.0)
        #            )
        #            self.AIRPRESS = cover(
        #                self.wf_readmap(self.AIRPRESS_mapstack, default=980.0), scalar(980.0)
        #            )
        #            # print "Using climatology for wind, air pressure and albedo."
        #        elif self.UseETPdata == 0:
        #            self.TMIN = cover(
        #                self.wf_readmap(self.TMIN_mapstack, 10.0), scalar(10.0)
        #            )  # T in degC
        #            self.TMAX = cover(
        #                self.wf_readmap(self.TMAX_mapstack, 10.0), scalar(10.0)
        #            )  # T in degC
        #            self.RAD = cover(
        #                self.wf_readmap(self.RAD_mapstack, 10.0), scalar(10.0)
        #            )  # W m-2 s-1
        #            self.WINDSPEED = cover(
        #                self.wf_readmap(self.WINDSPEED_mapstack, 10.0), scalar(10.0)
        #            )  # ms-1
        #            self.AIRPRESS = cover(
        #                self.wf_readmap(self.AIRPRESS_mapstack, 980.0), scalar(980.0)
        #            )  # Pa
        #            self.ALBEDO = cover(
        #                self.wf_readmapClimatology(self.ALBEDO_mapstack, default=0.1),
        #                scalar(0.1),
        #            )

        self.wf_multparameters()
        doy = self.currentdatetime.timetuple().tm_yday

        # conversion daylength
        pcr.setglobaloption("radians")
        m = pcr.scalar(1) - pcr.tan(
            (self.latitude * pcr.scalar(math.pi) / pcr.scalar(180))
        ) * pcr.tan(
            (
                (pcr.scalar(23.439) * pcr.scalar(math.pi) / pcr.scalar(180))
                * pcr.cos(
                    pcr.scalar(2)
                    * pcr.scalar(math.pi)
                    * (doy + pcr.scalar(9))
                    / pcr.scalar(365.25)
                )
            )
        )
        self.fday = pcr.min(
            pcr.max(
                pcr.scalar(0.02),
                pcr.scalar(
                    pcr.acos(
                        pcr.scalar(1)
                        - pcr.min(pcr.max(pcr.scalar(0), m), pcr.scalar(2))
                    )
                )
                / pcr.scalar(math.pi),
            ),
            pcr.scalar(1),
        )  # fraction daylength

        # Assign forcing and estimate effective meteorological variables

        Pg = self.PRECIP  # mm

        if self.UseETPdata == 1:
            Ta = self.TDAY  # T in degC
            T24 = self.TDAY  # T in degC
        elif self.UseETPdata == 0:
            Rg = pcr.max(
                self.RAD, pcr.scalar(0.0001)
            )  # already in W m-2 s-1; set minimum of 0.01 to avoid numerical problems
            Ta = self.TMIN + pcr.scalar(0.75) * (self.TMAX - self.TMIN)  # T in degC
            T24 = self.TMIN + pcr.scalar(0.5) * (self.TMAX - self.TMIN)  # T in degC
            pex = pcr.min(
                pcr.scalar(17.27) * (self.TMIN) / (pcr.scalar(237.3) + self.TMIN),
                pcr.scalar(10),
            )  # T in degC
            pe = pcr.min(
                pcr.scalar(610.8) * (pcr.exp(pex)), pcr.scalar(10000.0)
            )  # Mean actual vapour pressure, from dewpoint temperature
        # windspeed is at 1m
        # u2 = scalar(WindFactor)*self.WINDSPEED*(scalar(1)-(scalar(1)-self.fday)*scalar(0.25))/self.fday
        self.u1 = (
            self.WINDSPEED
            * (pcr.scalar(1) - (pcr.scalar(1) - self.fday) * pcr.scalar(0.25))
            / self.fday
        )

        pair = self.AIRPRESS  # already in Pa
        pes = 610.8 * pcr.exp(17.27 * Ta / (237.3 + Ta))

        # diagnostic equations
        w0 = self.S0 / self.S0max  # (2.1)
        ws = self.Ss / self.Ssmax  # (2.1)
        wd = self.Sd / self.Sdmax  # (2.1)

        # Calculate vegetation parameters and cover fractions
        # TODO: change to include clim maps of LAI and fveg?
        self.LAI = self.SLA * self.Mleaf  # (5.3)
        fveg = pcr.max(1 - pcr.exp(-self.LAI / self.LAIref), 0.000001)  # (5.3)
        fsoil = 1 - fveg
        LUE = self.LUEmax * self.Vc * fveg

        # Calculate open water fraction
        ChannelSurface = pcr.min(0, (0.007 * self.Sr**0.75))
        OpenWaterFrac = pcr.max(ChannelSurface, self.OpenWaterFrac)

        # Calculate snow cover fraction
        TotSnow = self.FreeWater + self.DrySnow
        fsnow = pcr.min(1.0, 0.05 * TotSnow)  # assumed; more analysis needed

        # V5 'HANDometric' equations
        # requires self.porosity, self.HAND, self.perc_HAND
        z_g = self.HAND[0] + pcr.pcr2numpy(
            self.Sg / (self.porosity * 1000), np.nan
        )  # groundwater table height in m AMSL (Sg=0 equates to drainage base)
        # saturated area (considers capillary rise, hence +0.3 m)
        z = self.HAND[0] + pcr.pcr2numpy(
            (self.Sg / (self.porosity * 1000) + (-self.psi_s)), np.NaN
        )  # bubbling pressure as indication of capillary fringe
        fg = interp_hand(z, self.HAND, self.perc_HAND) / 100.0

        # same for veg with access to gw
        RD = 1.0  # assumed maximum depth of shallow root water uptake
        z = self.HAND[0] + pcr.pcr2numpy((self.Sg / (self.porosity * 1e3)), np.nan) + RD
        fUgShallow = (interp_hand(z, self.HAND, self.perc_HAND) / 100.0) * (
            1.0 - self.fPotDeep
        )
        RD = 7.0  # assumed maximum depth of deep root water uptake
        z = self.HAND[0] + pcr.pcr2numpy(self.Sg / (self.porosity * 1e3), np.nan) + RD
        fUgDeep = interp_hand(z, self.HAND, self.perc_HAND) / 100 * self.fPotDeep
        fUg = fUgShallow + fUgDeep

        # Spatialise these fractions (largely superfluous with 1 HRU)
        # Rewrite this part if > 1 HRU
        fw_local = ChannelSurface
        fwater = OpenWaterFrac
        fsat = pcr.min(1, pcr.max(OpenWaterFrac, fg))  ## V5

        # Aerodynamic conductance (3.7)
        fh = pcr.ln(
            813 / pcr.max(0.25, self.hveg) - 5.45
        )  # assume minimum roughness of 0.25 m
        # ADJUSTED FOR E2O WFEI DATA: uz at 1m screen height (see AWRA technical report)
        ku1 = 0.359 / (fh * (fh + 2.3))
        ga = pcr.max(0.001, ku1 * self.u1)  # minimum of 0.001 imposed to avoid issues

        if self.UseETPdata == 1:
            self.E0 = pcr.max(self.EPOT, 0)
            self.keps = (
                0.655e-3 * pair / pes
            )  # See Appendix A3 (http://www.clw.csiro.au/publications/waterforahealthycountry/2010/wfhc-aus-water-resources-assessment-system.pdf) --------------------------------   check!
            self.Ept = self.E0

        elif self.UseETPdata == 0:
            # CALCULATION OF PET
            # Conversions and coefficients (3.1)
            fRH = (
                pe / pes
            )  # relative air humidity                                  -------------- check
            cRE = 0.03449 + 4.27e-5 * Ta
            Caero = (
                0.176 * (1 + Ta / 209.1) * (pair - 0.417 * pe) * (1 - fRH)
            )  # removed fday as already daytime
            self.keps = (
                1.4e-3 * ((Ta / 187) ** 2 + Ta / 107 + 1) * (6.36 * pair + pe) / pes
            )
            Rgeff = Rg / self.fday  # this is original

            # albedo model
            alb_veg = self.f_alb_Vc * self.Vc
            dryfrac = pcr.exp(-w0 / self.w0ref_alb) * (1 - fsat)
            alb_soil = self.alb_wet + (self.alb_dry - self.alb_snow) * dryfrac
            alb_ns = fveg * alb_veg + fsoil * alb_soil
            alb = (
                (1 - fwater) * (1 - fsnow) * alb_ns
                + fsnow * self.alb_snow
                + fwater * self.alb_water
            )

            RSn = (1 - alb) * Rgeff

            # long wave radiation balance (3.3 to 3.5)
            StefBolz = 5.67e-8
            Tkelv = Ta + 273.16

            RLin = (
                self.LWdown
            )  # provided by E2O data (though not sure how good it is..)
            RLout = 1 * StefBolz * Tkelv**4  # v0.5   # (3.4)
            RLn = RLin - RLout

            self.fGR = self.Gfrac_max * (1 - pcr.exp(-fsoil / self.fvegref_G))
            self.Rneff = pcr.max(
                1, (RSn + self.RLn) * (1 - self.fGR)
            )  # original (assuming any condensation is already measured in rain and there is a minimum Rneff of 1 W/m2 (to prevent any zero issues)

            # Potential evaporation (original)
            kalpha = pcr.min(
                1.4, 1 + Caero * ga / self.Rneff
            )  # do not allow value higher as that implies a unlikely high rate of advection from nearby areas only likely to occur for wet canopy.
            self.E0 = (
                cRE * (1 / (1 + self.keps)) * kalpha * self.Rneff * self.fday
            )  # for canopy
            self.Ept = (
                cRE * (1 / (1 + self.keps)) * 1.26 * self.Rneff * self.fday
            )  # for open water

        # CALCULATION OF ET FLUXES AND ROOT WATER UPTAKE
        # Root water uptake constraint (4.4)
        # For v5 no Uomax so temporarily bypassed here
        U0max = pcr.scalar(0)
        Usmax = pcr.max(
            0, self.Us0 * pcr.min(1, ws / self.wslimU)
        )  ##0-waarden omdat ws1 bevat 0-waarden (zie regel 116)
        Udmax = pcr.max(
            0, self.Ud0 * pcr.min(1, wd / self.wdlimU)
        )  ##0-waarden omdat wd1 bevat 0-waarden (zie regel 118)
        Ugmax = pcr.max(0, self.Ug0 * pcr.max(0, fUg - fsat))
        Umax = pcr.max(Usmax, pcr.max(Udmax, Ugmax))

        # Maximum transpiration (4.3)
        Gsmax = self.Gs_scalar * self.cGsmax * self.Vc

        if self.UseETPdata == 1:
            fD = 1.0
        elif self.UseETPdata == 0:
            VPD = pcr.max(0, pes - pe)
            fD = self.Cg / (1 + VPD / self.D50)

        gs = fveg * fD * Gsmax
        ft = 1 / (1 + (self.keps / (1 + self.keps)) * ga / gs)
        Etmax = ft * self.E0

        # Actual transpiration (4.1)
        Et = pcr.min(Umax, Etmax)

        # # Root water uptake distribution (2.3)
        # # Below seems to be in v5
        U0 = pcr.scalar(0)
        Us = pcr.max(0, pcr.min((Usmax / (Usmax + Udmax + Ugmax)) * Et, self.Ss - 1e-2))
        Ud = pcr.max(0, pcr.min((Udmax / (Usmax + Udmax + Ugmax)) * Et, self.Sd - 1e-2))
        Ug = pcr.max(0, pcr.min((Ugmax / (Usmax + Udmax + Ugmax)) * Et, self.Sd - 1e-2))

        Et = U0 + Us + Ud + Ug  # to ensure mass balance

        # Soil evaporation (4.5)
        w0x = pcr.max(0, (self.S0 - U0) / self.S0max)  # adjusted top soil water content
        fsoilE = self.FsoilEmax * pcr.min(1, w0x / self.w0limE)
        Es0 = (1 - fsat) * fsoilE * (pcr.max(0, self.E0 - Et))

        # Groundwater evaporation (4.6)
        Eg0 = pcr.max(0, fsat - fwater) * self.FsoilEmax * pcr.max(0, self.E0 - Et)
        Es = Es0 + Eg0

        # Open water evaporation (4.7) # uses Priestley-Taylor
        self.Erl = fw_local * self.FwaterE * self.Ept  # from local river channels
        Err = (fwater - fw_local) * self.FwaterE * self.Ept  # from remaining open water
        Er = self.Erl + Err

        # Rainfall interception evaporation (4.2)
        Sveg = self.S_sls * self.LAI
        fER = fveg * self.ER_coeff * pcr.max(0.05, self.hveg) ** self.ER_exp
        Pwet = pcr.max(
            0,
            (
                pcr.scalar((Sveg > 0) & (fER > 0) & ((fER / fveg) < 1))
                * -pcr.ln(1 - fER / fveg)
                * Sveg
                / fER
            ),
        )
        Ei = pcr.scalar(T24 > 0) * (
            pcr.scalar(Pg < Pwet) * fveg * Pg
            + pcr.scalar(Pg >= Pwet) * (fveg * Pwet + fER * (Pg - Pwet))
        )

        Edry = Et + Es + Er
        self.EACT = Edry + Ei  # for output only

        # HBV snow routine
        # Matlab: function [FreeWater,DrySnow,InSoil]=snow_submodel(Precipitation,Temperature,FreeWater,DrySnow)
        # derived from HBV-96 shared by Jaap Schellekens (Deltares) in May 2011
        # original in PCraster, adapted to Matlab by Albert van Dijk
        # HBV snow routine
        self.Pn = Pg - Ei

        # Snow routine parameters
        # parameters

        # Partitioning into fractions rain and snow
        Temperature = T24  # Dimmie, let op: tijdelijke regel!!
        RainFrac = pcr.max(
            0,
            pcr.min(
                (Temperature - (self.snow_TT - self.snow_TTI / 2)) / self.snow_TTI, 1
            ),
        )
        SnowFrac = 1 - RainFrac  # fraction of precipitation which falls as snow

        # Snowfall/melt calculations
        SnowFall = SnowFrac * self.Pn  # snowfall depth
        RainFall = RainFrac * self.Pn  # rainfall depth
        PotSnowMelt = self.snow_Cfmax * pcr.max(
            0, Temperature - self.snow_TT
        )  # Potential snow melt, based on temperature
        PotRefreezing = (
            self.snow_Cfmax * self.snow_Cfr * pcr.max(self.snow_TT - Temperature, 0)
        )  # Potential refreezing, based on temperature
        Refreezing = pcr.min(PotRefreezing, self.FreeWater)  # actual refreezing
        SnowMelt = pcr.min(PotSnowMelt, self.DrySnow)  # actual snow melt
        self.DrySnow = (
            self.DrySnow + SnowFall + Refreezing - SnowMelt
        )  # dry snow content
        self.FreeWater = self.FreeWater - Refreezing  # free water content in snow
        MaxFreeWater = self.FreeWater * self.snow_WHC
        self.FreeWater = self.FreeWater + SnowMelt + RainFall
        InSoil = pcr.max(
            self.FreeWater - MaxFreeWater, 0
        )  # abundant water in snow pack which goes into soil
        self.FreeWater = self.FreeWater - InSoil
        # End of Snow Module
        Rmelt = pcr.scalar(Temperature < 0) * InSoil  # runs off if soil still frozen
        self.Ps = pcr.scalar(Temperature >= 0) * InSoil

        # CALCULATION OF WATER BALANCES
        # surface water fluxes (2.2)
        Rsof = fsat * self.Ps
        Pi = pcr.max(0, self.Ps - self.InitLoss)
        Rhof_soil = pcr.max(0, 1 - fsat - self.fImp) * (
            Pi - self.Pref * pcr_tanh(Pi / self.Pref)
        )  # CHECK IF THIS GOES OK IN PYTHON ## v5 ##
        Rhof_imp = self.fImp * (
            Pi - self.Pref_imp * pcr_tanh(Pi / self.Pref_imp)
        )  # CHECK IF THIS GOES OK IN PYTHON
        Rhof = Rhof_soil + Rhof_imp
        self.QR = Rhof + Rsof + Rmelt  # combined runoff
        self.I = self.Ps - Rhof - Rsof

        # SOIL WATER BALANCES (2.1 & 2.4)

        # Soil hydrology from v5 (Viney et al., 2015) http://www.bom.gov.au/water/landscape/static/publications/Viney_et_al_2015_AWRA_L_5.0_model_description.pdf
        Kr_0s = self.K0sat / self.Kssat
        Rh_0s = pcr_tanh(self.slope_coeff * self.slope * w0) * pcr_tanh(
            self.Kr_coeff * (Kr_0s - 1.0) * w0
        )
        # general case
        Km = (self.K0sat * self.Kssat) ** 0.5
        A = Km / (self.S0max**2)
        B = 1
        C = -(self.S0 + self.I - Es)
        S0 = (-B + ((B**2 - 4 * A * C) ** 0.5)) / (2 * A)
        D0 = (1 - Rh_0s) * Km * ((S0 / self.S0max) ** 2)
        IF0 = Rh_0s * Km * ((S0 / self.S0max) ** 2)
        # depletion case
        imap = (self.S0 + self.I) <= Es
        Es = pcr.ifthenelse(imap, (self.S0 + self.I), Es)
        S0 = pcr.ifthenelse(imap, 0, S0)
        D0 = pcr.ifthenelse(imap, 0, D0)
        IF0 = pcr.ifthenelse(imap, 0, IF0)
        # saturation case
        imap = (self.S0max - self.S0 + self.K0sat) <= (self.I - Es)
        D0 = pcr.ifthenelse(imap, (1 - Rh_0s) * self.K0sat, D0)
        IF0 = pcr.ifthenelse(
            imap,
            Rh_0s * self.K0sat + (self.S0 - self.S0max - self.K0sat + self.I - Es),
            IF0,
        )
        S0 = pcr.ifthenelse(imap, self.S0max, S0)
        # enforce mass balance (there can be small numerical errors in quadratic equation)
        S0 = pcr.max(0, pcr.min(S0, self.S0max))
        massbal = self.S0 + self.I - Es - D0 - IF0 - S0
        D0 = D0 + (1 - Rh_0s) * massbal
        IF0 = IF0 + Rh_0s * massbal
        self.S0 = S0  # Update state

        # # Shallow root zone water balance (Ss) (2.4)
        Kr_sd = self.Kssat / self.Kdsat
        Rh_sd = pcr_tanh(self.slope_coeff * self.slope * ws) * pcr_tanh(
            self.Kr_coeff * (Kr_sd - 1) * ws
        )
        # general case
        Km = (self.Kssat * self.Kdsat) ** 0.5
        A = Km / (self.Ssmax**2)
        B = 1
        C = -(self.Ss + D0 - Us)
        Ss = (-B + ((B**2 - 4 * A * C) ** 0.5)) / (2 * A)
        Ds = (1 - Rh_sd) * Km * ((Ss / self.Ssmax) ** 2)
        IFs = Rh_sd * Km * ((Ss / self.Ssmax) ** 2)
        # depletion case
        imap = (Ss + D0) <= Us
        Us = pcr.ifthenelse(imap, (self.Ss + D0), Us)
        Ss = pcr.ifthenelse(imap, 0, Ss)
        Ds = pcr.ifthenelse(imap, 0, Ds)
        IFs = pcr.ifthenelse(imap, 0, IFs)
        # saturation case
        imap = (self.Ssmax - self.Ss + self.Kssat) <= (D0 - Us)
        Ds = pcr.ifthenelse(imap, (1 - Rh_sd) * self.Kssat, Ds)
        IFs = pcr.ifthenelse(
            imap,
            Rh_sd * self.Kssat + (self.Ss - self.Ssmax - self.Kssat + D0 - Us),
            IFs,
        )
        Ss = pcr.ifthenelse(imap, self.Ssmax, Ss)
        # enforce mass balance (for numerical & rounding errors)
        Ss = pcr.max(0, pcr.min(Ss, self.Ssmax))
        massbal = self.Ss + D0 - Us - Ds - IFs - Ss
        Ds = Ds + (1 - Rh_sd) * massbal
        IFs = IFs + Rh_sd * massbal
        self.Ss = Ss  # Update state

        # # Deep root zone water balance (Sd) (2.4)
        # general case
        A = self.Kdsat / (self.Sdmax**2)
        B = 1.0
        C = -(self.Sd + Ds - Ud)
        Sd = (-B + ((B**2 - 4 * A * C) ** 0.5)) / (2 * A)
        Dd = self.Kdsat * ((Sd / self.Sdmax) ** 2)
        IFd = 0 * Dd
        # depletion case
        imap = (Sd + Ds) <= Ud
        Ud = pcr.ifthenelse(imap, (self.Sd + Ds), Ud)
        Sd = pcr.ifthenelse(imap, 0, Sd)
        Dd = pcr.ifthenelse(imap, 0, Dd)
        # saturation case
        imap = (self.Sdmax - self.Sd + self.Kdsat) <= (Ds - Ud)
        Dd = pcr.ifthenelse(imap, self.Kdsat, Dd)
        IFd = pcr.ifthenelse(imap, (self.Sd - self.Sdmax - self.Kdsat + Ds - Ud), IFd)
        Sd = pcr.ifthenelse(imap, self.Sdmax, Sd)
        # enforce mass balance (for numerical & rounding errors
        Sd = pcr.max(0, pcr.min(Sd, self.Sdmax))
        massbal = self.Sd + Ds - Ud - Dd - IFd - Sd
        Dd = Dd + massbal
        self.Sd = Sd  # Update state

        IFs = IFs + IFd  # add up to interflow
        self.QR = self.QR + IF0 + IFs  # add to runoff

        # CATCHMENT WATER BALANCE
        # Groundwater store water balance (Sg) (2.5)
        NetGf = Dd - Eg0 - Ug
        self.Sg = self.Sg + NetGf
        Sg_fd = pcr.max(self.Sg, 0)
        self.Qg = pcr.min(Sg_fd, (1 - pcr.exp(-self.K_gw)) * Sg_fd)
        self.Sg = self.Sg - self.Qg

        # Surface water store water balance (Sr) (2.7)
        self.Sr = pcr.max(0, self.Sr + self.QR - self.Erl + self.Qg)
        self.Qtot = pcr.max(0, pcr.min(self.Sr, (1 - pcr.exp(-self.K_rout)) * self.Sr))
        # Cover in case there is no data in the subcatch
        self.Qtot = pcr.ifthen(self.subcatch > 0, pcr.cover(self.Qtot, 0.0))
        self.Sr = self.Sr - self.Qtot

        # VEGETATION ADJUSTMENT (5.7-5.8)

        fvmax = 1 - pcr.exp(-pcr.max(self.LAImax, 0.002778) / self.LAIref)
        self.fveq = (
            (1 / pcr.max((self.E0 / Umax) - 1, 1e-3))
            * (self.keps / (1 + self.keps))
            * (ga / (fD * Gsmax))
        )
        self.fveq = pcr.min(self.fveq, fvmax)

        # VEGETATION ADJUSTMENT (5.4-5.6)
        dMleaf = -pcr.ln(1 - self.fveq) * self.LAIref / self.SLA - self.Mleaf
        Mleafnet = (
            pcr.scalar(dMleaf > 0) * (dMleaf / self.Tgrow)
            + pcr.scalar(dMleaf < 0) * dMleaf / self.Tsenc
        )
        self.Mleaf = self.Mleaf + Mleafnet

        self.LAI = self.SLA * self.Mleaf  # (5.3)

        fveg = 1 - pcr.exp(-self.LAI / self.LAIref)  # (5.3)
        # in case this is desired as output:
        self.w0 = self.S0 / self.S0max  # (2.1)
        self.TotSnow = self.DrySnow + self.FreeWater


# The main function is used to run the program from the command line


def main(argv=None):
    """
    *Optional*

    Perform command line execution of the model. This example uses the getopt
    module to parse the command line options.

    The user can set the caseName, the runDir, the timestep and the configfile.
    """
    global multpars
    caseName = "openstreams_w3"  # "D:/trambaue/_Projects/GLOFFIS/201501/GLOFFIS_SA/Modules/openstreams_w3ra/"
    runId = "run_default"
    configfile = "wflow_w3.ini"
    _lastTimeStep = 0
    _firstTimeStep = 0
    timestepsecs = 86400

    wflow_cloneMap = "wflow_subcatch.map"
    runinfoFile = "runinfo.xml"
    _NoOverWrite = False
    loglevel = logging.DEBUG
    LogFileName = "wflow.log"

    # This allows us to use the model both on the command line and to call
    # the model usinge main function from another python script.

    if argv is None:
        argv = sys.argv[1:]
        if len(argv) == 0:
            usage()
            return

    opts, args = getopt.getopt(argv, "C:S:T:c:s:R:")

    for o, a in opts:

        if o == "-C":
            caseName = a
        if o == "-R":
            runId = a
        if o == "-c":
            configfile = a
        if o == "-s":
            timestepsecs = int(a)

    starttime = dt.datetime(1990, 1, 1)

    if _lastTimeStep < _firstTimeStep:
        print(
            "The starttimestep ("
            + str(_firstTimeStep)
            + ") is smaller than the last timestep ("
            + str(_lastTimeStep)
            + ")"
        )
        usage()

    myModel = WflowModel(wflow_cloneMap, caseName, runId, configfile)
    dynModelFw = wf_DynamicFramework(
        myModel, _lastTimeStep, firstTimestep=_firstTimeStep, datetimestart=starttime
    )
    dynModelFw.createRunId(
        NoOverWrite=_NoOverWrite,
        level=loglevel,
        logfname=LogFileName,
        model="wflow_w3",
        doSetupFramework=False,
    )

    for o, a in opts:
        if o == "-P":
            left = a.split("=")[0]
            right = a.split("=")[1]
            configset(
                myModel.config, "variable_change_once", left, right, overwrite=True
            )
        if o == "-p":
            left = a.split("=")[0]
            right = a.split("=")[1]
            configset(
                myModel.config, "variable_change_timestep", left, right, overwrite=True
            )
        if o == "-X":
            configset(myModel.config, "model", "OverWriteInit", "1", overwrite=True)
        if o == "-I":
            configset(myModel.config, "run", "reinit", "1", overwrite=True)
        if o == "-i":
            configset(myModel.config, "model", "intbl", a, overwrite=True)
        if o == "-s":
            configset(myModel.config, "model", "timestepsecs", a, overwrite=True)
        if o == "-T":
            configset(myModel.config, "run", "endtime", a, overwrite=True)
        if o == "-S":
            configset(myModel.config, "run", "starttime", a, overwrite=True)

    dynModelFw.setupFramework()

    dynModelFw._runInitial()
    dynModelFw._runResume()
    # dynModelFw._runDynamic(0,0)
    dynModelFw._runDynamic(_firstTimeStep, _lastTimeStep)
    dynModelFw._runSuspend()
    dynModelFw._wf_shutdown()


if __name__ == "__main__":
    main()
