# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
EventSource for NectarCAM protobuf-fits.fz-files.

Needs protozfits v1.5.0 from github.com/cta-sst-1m/protozfitsreader
"""

import glob
import numpy as np
import struct
from astropy import units as u
from astropy.io import fits
from astropy.time import Time
from ctapipe.containers import (
    PixelStatusContainer, EventType, R0CameraContainer, R1CameraContainer,
    CoordinateFrameType, PointingMode, SchedulingBlockContainer, ObservationBlockContainer, # VIM : line of things in lst equivalent
)
from ctapipe.coordinates import CameraFrame
from ctapipe.core import Provenance
from ctapipe.core.traits import Int, Bool, Enum
from ctapipe.instrument import (
    TelescopeDescription,
    SubarrayDescription,
    CameraDescription,
    CameraReadout,
    CameraGeometry,
    OpticsDescription,
    SizeType,
    ReflectorShape
)

from ctapipe.io import EventSource, DataLevel
from enum import IntFlag, auto
from pkg_resources import resource_filename

from .calibration import NectarCAMR0Corrections
from .constants import (
    N_GAINS, N_PIXELS, N_SAMPLES
)
from .containers import NectarCAMDataContainer, NectarCAMServiceContainer, \
    NectarCAMEventContainer
from .anyarray_dtypes import (
    CDTS_AFTER_37201_DTYPE,
    CDTS_BEFORE_37201_DTYPE,
    TIB_DTYPE,
)
from .version import __version__

__all__ = ['NectarCAMEventSource', '__version__']

S_TO_NS = np.uint64(1e9)

class TriggerBits(IntFlag):
    '''
    See TIB User manual
    '''
    UNKNOWN = 0
    MONO = auto()
    STEREO = auto()
    CALIBRATION = auto()
    SINGLE_PE = auto()
    SOFTWARE = auto()
    PEDESTAL = auto()
    SLOW_CONTROL = auto()

    PHYSICS = MONO | STEREO
    OTHER = CALIBRATION | SINGLE_PE | SOFTWARE | PEDESTAL | SLOW_CONTROL


class PixelStatus(IntFlag):
    '''
    Pixel status information
    See Section A.5 of the CTA R1 Data Model:
    https://forge.in2p3.fr/dmsf/files/8627
    '''
    RESERVED_0 = auto()
    RESERVED_1 = auto()
    HIGH_GAIN_STORED = auto()
    LOW_GAIN_STORED = auto()
    SATURATED = auto()
    PIXEL_TRIGGER_1 = auto()
    PIXEL_TRIGGER_2 = auto()
    PIXEL_TRIGGER_3 = auto()

    BOTH_GAINS_STORED = HIGH_GAIN_STORED | LOW_GAIN_STORED


OPTICS = OpticsDescription(
    # https://gitlab.cta-observatory.org/cta-science/simulations/simulation-model/verification/verification-process/mst-structure/-/blob/master/Appendix-MST-Structure.pdf
    # version from 20 Jan 2022
    'MST',
    size_type=SizeType.MST,
    equivalent_focal_length=u.Quantity(16., u.m),
    effective_focal_length=u.Quantity(16.44505,u.m), # from https://www.mpi-hd.mpg.de/hfm/CTA/MC/Prod5/Config/PSF/fov_prod4.pdf mentioned in the link above
    n_mirrors=1,
    mirror_area=u.Quantity(106., u.m ** 2),  # no shadowing, uncertainty is 0.5 m2
    n_mirror_tiles=86,  # Garczarczyk 2017
    reflector_shape=ReflectorShape.HYBRID #according to Dan Parsons
)



def nectar_trigger_patches(geometry,pix_mid_module):
    '''
    Input:
        camera geometry  
            e.g. `source.subarray.tel[10].camera.geometry`
        pix_mid_module
            list of pixels at the centre of the modules (from `module_central` routine)
    Returns:
        trigger_patches
            list of trigger_patches, each consisting of a list of pixels in that patch
        
    Requires to have 7-pixel modules.
    Neighbours gets lazy-loaded by the main ctapipe function in camera.geometry.
    '''
    
    from copy import copy
    from scipy import sparse    

    trigger_patches = []    

    for patch_pix in pix_mid_module:
        trigger_patch = patch_pix
        # Use a set to avoid repeating pixels
        trigger_patch = set([patch_pix]+geometry.neighbors[patch_pix])
        for nbrs in geometry.neighbors[patch_pix]:
            trigger_patch |= set(geometry.neighbors[nbrs])
        for nbrs in copy(trigger_patch):
            trigger_patch |= set(geometry.neighbors[nbrs])
        trigger_patch = list(trigger_patch)
        #type(trigger_patch),trigger_patch,len(trigger_patch)
                
        # Just for tidiness
        trigger_patch.sort()
        trigger_patches.append(trigger_patch)
    
        
    return trigger_patches

def module_central(geometry):
    '''
    Input:
        camera geometry  
            e.g. `source.subarray.tel[10].camera.geometry`
    Returns:
        pix_mid_module
            list of pixels at the centre of each module (sorted)
        
    Requires the geometry to have pixel_x and pixel_y, and to have 7-pixel modules.
    Neighbours gets lazy-loaded by the main ctapipe function in camera.geometry.
    
    Find central pixels of all the modules, as follows:

    Finds the most distant PMT and which still has 6 neighbours, 
      which must be the central pixel of a module.
    Then elimates that module and repeats, until no modules left.

    '''
    
    import numpy as np
    from copy import copy,deepcopy
    
    # Bug out right away if the number of pixels is not divisible by 7 (for 7-pixel modules)
    if geometry.n_pixels%7 != 0:
        raise ValueError("n_pixels is not divisible by 7, so not full 7-pixel modules. \n"+
            "... Incompatible with expected NectarCAM geometry.")
    
    pix_mid_module = []
    
    pixel_dist = np.hypot(geometry.pix_x,geometry.pix_y).value

    # Make a dictionary by pixel index, so that pixels can be deleted later
    pixel_dist_dict = dict(zip(range(geometry.n_pixels),pixel_dist))
    
    # geometry.neighbours gets lazy-loaded by the main ctapipe function in camera.geometry
    pixel_nbrs_dict = dict(zip(range(geometry.n_pixels),deepcopy(geometry.neighbors)))

    while len(pixel_dist_dict)>0:

        # Find the farthest pixel which still has 6 neighbours
        
        # This holds the index within the dictionary
        pixdist_order = np.argsort(list(pixel_dist_dict.values()))

        for pix_idx in pixdist_order[::-1]: # Go backwards
            pix_key = list(pixel_dist_dict.keys())[pix_idx]
            if len(pixel_nbrs_dict[pix_key]) == 6:
                break
        pix_cent = pix_key
        
        pix_mid_module.append(pix_cent)
        
        module = [pix_cent] + pixel_nbrs_dict[pix_key]
        
        for pix_key in module:
            
            # remove all the current module pixels from the neighbours list values
            for nbr_key in pixel_nbrs_dict:
                if pix_key in pixel_nbrs_dict[nbr_key]:
                    pixel_nbrs_dict[nbr_key].remove(pix_key)

            # remove the current module pixels from the neighbours dictionary itself
            if pix_key in pixel_nbrs_dict:
                pixel_nbrs_dict.pop(pix_key)
            
            # remove the current module pixels from the distance dictionary
            if pix_key in pixel_dist_dict:
                pixel_dist_dict.pop(pix_key)
                
    # Bug out right away if the number of central pixels is not == n_pixels/7 (for 7-pixel modules)
    if len(pix_mid_module) != geometry.n_pixels/7:
        raise ValueError("Number of module central pixels != n_pixels/7. \n"+
            "... Incompatible with expected NectarCAM geometry.")

                
    # Just for tidiness
    pix_mid_module.sort()
        
    return pix_mid_module

def find_central_pixels(cam: CameraGeometry):
    '''
    Input:
        camera geometry  
            e.g. `source.subarray.tel[10].camera.geometry`
    Returns:
        central_pixel_ids
            list of pixels at the centre of each module (sorted)
        
    Requires the geometry to have pixel_x and pixel_y, and to have 7-pixel modules.
    Neighbours gets lazy-loaded by the main ctapipe function in camera.geometry.
    
    Find central pixels of all the modules, as follows:

    Finds the most distant PMT and which still has 6 neighbours, 
      which must be the central pixel of a module.
    Then elimates that module and repeats, until no modules left.

    '''
    # Max's version of the above

    # Bug out right away if the number of pixels is not divisible by 7 (for 7-pixel modules)
    if cam.n_pixels%7 != 0:
        raise ValueError("n_pixels is not divisible by 7, so not full 7-pixel modules. \n"+
                         " -> Incompatible with expected NectarCAM geometry.")

    # distance to the camera center
    distance = np.hypot(cam.pix_x, cam.pix_y).to_value(cam.pix_x.unit)

    # pixel indices sorted from farthest to closest to the center
    order = np.argsort(distance)[::-1]

    # While not all pixels are visited, do
    # * Next pixel center is the furthest pixel from the center that has 6 neighbors
    # * Remove that pixel and its neighbors from the calculation
    central_pixel_ids = []

    neighbors = cam.neighbor_matrix.copy()
    n_neighbors = neighbors.sum(axis=0)
    n_unassigned = cam.n_pixels

    while n_unassigned > 0:
        # Next pixel center is the furthest pixel from the center that has 6 neighbors
        try:
            center = order[(n_neighbors[order] == 6)][0]
        except IndexError:
            print(center)
            raise IndexError("Search for module central pixels failed.\n"+
                             " -> Probably incompatible with expected NectarCAM geometry")

        central_pixel_ids.append(center)

        # remove that pixel and the pixels that belong to this module, i.e. the neighbors
        # of the central pixel, from the calculation
        other = neighbors[center]
        n_neighbors[center] -= 7
        n_neighbors -= neighbors[other].sum(axis=0)

        neighbors[:, center] = False
        neighbors[center, :] = False
        neighbors[:, other] = False
        neighbors[other, :] = False

        n_unassigned -= 7

    # Bug out right away if the number of central pixels is not == n_pixels/7 (for 7-pixel modules)
    if len(central_pixel_ids) != cam.n_pixels/7:
        raise ValueError("Number of module central pixels != n_pixels/7. \n"+
                         " -> Incompatible with expected NectarCAM geometry.")

    central_pixel_ids = np.array(central_pixel_ids)
    central_pixel_ids.sort()

    return central_pixel_ids

def add_nectar_trigger_patches_to_geom(cam: CameraGeometry):
    '''
        Does what it says on the tin
        Adds the trigger patches as an attribute on-the-fly for now.
            Input:
        camera geometry  
            e.g. `source.subarray.tel[10].camera.geometry`
        Returns:
            No return, just "cam" gets modified with added: 
            * pix_mid_module: list of pixel IDs
            * trigger_patches: list of list of pixel IDs
            * trigger_patches_mask: list of list of boolean masks (n_pixels x n_pixels)
            * trigger_patches_mask_sparse: sparse array of trigger_patches
    '''
    from scipy.sparse import csr_matrix, lil_matrix
    
    # Find central pixels of all the modules
    pix_mid_module = find_central_pixels(cam)
    cam.pix_mid_module = pix_mid_module
    # Get a list of trigger patches, with the PMTs in each patch (up to 37 = 7 + 6*5, but fewer at edges)
    trigger_patches = nectar_trigger_patches(cam,pix_mid_module)
    cam.trigger_patches = trigger_patches
    
    # Using a mask can be faster (tested with %%timeit), so calculate it here

    # Usage then is as follows:
    # e.g. # sum(np.array(patch_masks) & np.array(mask_trig)) is the number of patches triggered
    # > triggers = (np.sum(np.array(patch_masks) & np.array(mask_trig),axis=1))
    # > patch_count = sum(triggers>2)
    # # Where patch_count is the number of patches in triggered.
                
    patch_masks = []
    for patch in trigger_patches:
        patch_mask = np.array([False]*cam.n_pixels)
        patch_mask[np.array(patch)] = True
        patch_masks.append(patch_mask)        
    cam.trigger_patches_mask = patch_masks
    
    # Max suggestion to use sparse array
    # Usage as above, but need to convert from sparse array to array with patch_masks_sparse.toarray()
    # ... and then it is 4 times slower than using just the boolean mask array
    lil = lil_matrix((cam.n_pixels, cam.n_pixels), dtype=bool)
    for pix_id, patches in enumerate(trigger_patches):
        lil[pix_id, patches] = True
    patch_masks_sparse = lil.tocsr()
    cam.trigger_patches_mask_sparse = patch_masks_sparse
    
    return

def load_camera_geometry(version=3):
    ''' Load camera geometry from bundled resources of this repo,
         and find central pixels of the modules and trigger patches.'''
         
    #from ctapipe_io_nectarcam import nectar_trigger_patches,find_central_pixels
    
    f = resource_filename(
        'ctapipe_io_nectarcam', f'resources/NectarCam-{version:03d}.camgeom.fits.gz'
    )
    Provenance().add_input_file(f, role="CameraGeometry")
    geom = CameraGeometry.from_table(f)
    geom.frame = CameraFrame(focal_length=OPTICS.equivalent_focal_length)

    # Add the trigger patches as an attribute on-the-fly for now.
    add_nectar_trigger_patches_to_geom(geom)    
    
    return geom


def read_pulse_shapes():
    '''
    Reads in the data on the pulse shapes from an external file
    Returns
    -------
    (daq_time_per_sample, pulse_shape_time_step, pulse shapes)
        daq_time_per_sample: time between samples in the actual DAQ (ns, astropy quantity)
        pulse_shape_time_step: time between samples in the returned single-p.e pulse shape (ns, astropy
    quantity)
        pulse shapes: Single-p.e. pulse shapes, ndarray of shape (2, 1640)
    '''

    # https://gitlab.cta-observatory.org/cta-consortium/aswg/simulations/simulation-model/simulation-model-description/-/blob/master/datFiles/Pulse_template_nectarCam_17042020.dat
    infilename = resource_filename(
        'ctapipe_io_nectarcam',
        'resources/Pulse_template_nectarCam_17042020.dat'
    )

    data = np.genfromtxt(infilename, dtype='float', comments='#')
    Provenance().add_input_file(infilename, role="PulseShapes")
    pulse_shape_time_step = 0.125 * u.ns  # file specific, change if model file is changed
    # TODO read automatically from file

    # https://gitlab.cta-observatory.org/cta-science/simulations/simulation-model/verification/verification-process/mst-nectarcam/-/blob/master/Appendix-NectarCam.pdf
    # version from 13 Jan 2022
    daq_time_per_sample = 1. * u.ns

    # Note we have to transpose the pulse shapes array to provide what ctapipe
    # expects:
    return daq_time_per_sample, pulse_shape_time_step, data[:, 1:].T


def time_from_unix_tai_ns(unix_tai_ns):
    """
    Create an astropy Time instance from a unix time tai timestamp in ns.
    By using both arguments to time, the result will be a higher precision
    timestamp.
    """
    # make sure input is really uint64
    unix_tai_ns = np.asanyarray(unix_tai_ns, dtype=np.uint64)

    seconds, nanoseconds = np.divmod(unix_tai_ns, S_TO_NS)
    return Time(seconds, nanoseconds / S_TO_NS, format="unix_tai")

class NectarCAMEventSource(EventSource):
    """
    EventSource for NectarCam r0 data.
    """

    baseline = Int(
        250,
        help='r0 waveform baseline '
    ).tag(config=True)

    trigger_information = Bool(
        default_value=True,
        help='Fill trigger information.'
    ).tag(config=True)

    default_trigger_type = Enum(
        ['ucts', 'tib'], default_value='ucts',
        help=(
            'Default source for trigger type information.'
        )
    ).tag(config=True)

    calibrate_flatfields_and_pedestals = Bool(
        default_value=True,
        help='If True, flat field and pedestal events are also calibrated.'
    ).tag(config=True)

    def __init__(self, **kwargs):
        """
        Constructor
        Parameters
        ----------
        config: traitlets.loader.Config
            Configuration specified by config file or cmdline arguments.
            Used to set traitlet values.
            Set to None if no configuration to pass.
        tool: ctapipe.core.Tool
            Tool executable that is calling this component.
            Passes the correct logger to the component.
            Set to None if no Tool to pass.
        kwargs: dict
            Additional parameters to be passed.
            NOTE: The file mask of the data to read can be passed with
            the 'input_url' parameter.
        """
        # EventSource can not handle file wild cards as input_url
        # To overcome this we substitute the input_url with first file matching
        # the specified file mask (copied from  MAGICEventSourceROOT).

        if 'input_url' in kwargs.keys():
            self.file_list = glob.glob(str(kwargs['input_url']))
            self.file_list.sort()
            kwargs['input_url'] = self.file_list[0]
            super().__init__(**kwargs)
        elif 'input_filelist' in kwargs.keys():
            input_filelist = kwargs['input_filelist']
            if isinstance(input_filelist,str):
                self.file_list = [input_filelist]
            else:
                self.file_list = list(input_filelist)
            self.file_list.sort()
            kwargs['input_url'] = self.file_list[0]
            del kwargs['input_filelist']
            super().__init__(**kwargs)
        else:
            super().__init__(**kwargs)
            self.file_list = [self.input_url]

        self.multi_file = MultiFiles(self.file_list)
        self.camera_config = self.multi_file.camera_config
        self.log.info("Read {} input files".format(self.multi_file.num_inputs()))
        self.run_id = self.camera_config.configuration_id
        self.tel_id = self.camera_config.telescope_id #VIM : Change the _tel_id to tel_id to be consistent with lst
        self.run_start = Time(self.camera_config.date, format='unix')
        self.geometry_version = 3
        self._subarray = self.create_subarray(self.geometry_version, self.tel_id)
        self.r0_r1_calibrator = NectarCAMR0Corrections(
             subarray=self._subarray, parent=self
         )
        self.nectarcam_service = self.fill_nectarcam_service_container_from_zfile(self.tel_id,
                                                                                  self.camera_config)

        target_info = {}

        # VIM : Put some pointing info to be filled as a reminder that it should be done at some point
        #self.pointing_source = PointingSource(subarray=self.subarray, parent=self)
        pointing_mode = PointingMode.UNKNOWN
        #if self.pointing_information:
        #    target = self.pointing_source.get_target(tel_id=self.tel_id, time=self.run_start)
        #    if target is not None:
        #        target_info["subarray_pointing_lon"] = target["ra"]
        #        target_info["subarray_pointing_lat"] = target["dec"]
        #        target_info["subarray_pointing_frame"] = CoordinateFrameType.ICRS
        #        pointing_mode = PointingMode.TRACK

        self._scheduling_blocks = {
            self.run_id: SchedulingBlockContainer(
                sb_id=np.uint64(self.run_id),
                producer_id=f"MST-{self.tel_id}",
                pointing_mode=pointing_mode,
            )
        }

        self._observation_blocks = {
            self.run_id: ObservationBlockContainer(
                obs_id=np.uint64(self.run_id),
                sb_id=np.uint64(self.run_id),
                producer_id=f"MST-{self.tel_id}",
                actual_start_time=self.run_start,
                **target_info
            )
        }

    def get_entries(self):
        tot_events = 0
        for v in self.multi_file._events_table.values():
            tot_events += len(v)
        return tot_events

    def __len__(self):
        return self.get_entries()

    @property
    def is_simulation(self):
        return False

    @property
    def datalevels(self):
        if self.r0_r1_calibrator.calibration_path is not None:
            return (DataLevel.R0, DataLevel.R1)
        return (DataLevel.R0,)

    @property
    def subarray(self):
        return self._subarray

    @staticmethod
    def create_subarray(geometry_version, tel_id=0):
        """
        Obtain the subarray from the EventSource
        Returns
        -------
        ctapipe.instrument.SubarrayDescription
        """

        # camera info from NectarCAM-[geometry_version].camgeom.fits.gz file
        camera_geom = load_camera_geometry(version=geometry_version)

        # get info on the camera readout:
        daq_time_per_sample, pulse_shape_time_step, pulse_shapes = read_pulse_shapes()

        camera_readout = CameraReadout(
            name = 'NectarCam',
            n_pixels=N_PIXELS,
            n_channels=N_GAINS,
            n_samples=N_SAMPLES,
            sampling_rate = (1 / daq_time_per_sample).to(u.GHz),
            reference_pulse_shape = pulse_shapes,
            reference_pulse_sample_width = pulse_shape_time_step,
        )

        camera = CameraDescription('NectarCam', camera_geom, camera_readout)

        mst_tel_descr = TelescopeDescription(
            name='NectarCam', optics=OPTICS, camera=camera
        )

        tel_descriptions = {tel_id: mst_tel_descr}

        # MST telescope position
        tel_positions = {tel_id: [0., 0., 0] * u.m}

        subarray = SubarrayDescription(
            name=f"MST-{tel_id} subarray",
            tel_descriptions=tel_descriptions,
            tel_positions=tel_positions,
        )

        return subarray


    @property
    def observation_blocks(self):
        return self._observation_blocks

    @property
    def scheduling_blocks(self):
        return self._scheduling_blocks

    @property
    def obs_ids(self):
        # currently no obs id is available from the input files
        return [self.camera_config.configuration_id, ]

    def _generator(self):

        # container for NectarCAM data
        array_event = NectarCAMDataContainer()
        array_event.meta['input_url'] = self.input_url
        array_event.meta['max_events'] = self.max_events
        array_event.meta['origin'] = 'NectarCAM'

        # also add service container to the event section
        array_event.nectarcam.tel[self.tel_id].svc = self.nectarcam_service

        # initialize general monitoring container
        self.initialize_mon_container(array_event)

        # loop on events
        for count, event in enumerate(self.multi_file):

            array_event.count = count
            array_event.index.event_id = event.event_id
            array_event.index.obs_id = self.obs_ids[0]

            # fill R0/R1 data
            self.fill_r0r1_container(array_event, event)
            # fill specific NectarCAM event data
            # fill specific NectarCAM event data
            self.fill_nectarcam_event_container_from_zfile(array_event, event)

            if self.trigger_information:
                self.fill_trigger_info(array_event)

            # fill general monitoring data
            self.fill_mon_container_from_zfile(array_event, event)

            # gain select and calibrate to pe
            if self.r0_r1_calibrator.calibration_path is not None:
                # skip flatfield and pedestal events if asked
                if (
                        self.calibrate_flatfields_and_pedestals
                        or array_event.trigger.event_type not in {EventType.FLATFIELD,
                                                                  EventType.SKY_PEDESTAL}
                ):
                    self.r0_r1_calibrator.calibrate(array_event)

            yield array_event

    @staticmethod
    def is_compatible(file_path):
        try:
            # The file contains two tables:
            #  1: CameraConfig
            #  2: Events
            h = fits.open(file_path)[2].header
            ttypes = [
                h[x] for x in h.keys() if 'TTYPE' in x
            ]
        except OSError:
            # not even a fits file
            return False

        except IndexError:
            # A fits file of a different format
            return False

        is_protobuf_zfits_file = (
                (h['XTENSION'] == 'BINTABLE') and
                (h['EXTNAME'] == 'Events') and
                (h['ZTABLE'] is True) and
                (h['ORIGIN'] == 'CTA') and
                (h['PBFHEAD'] == 'R1.CameraEvent')
        )

        is_nectarcam_file = 'nectarcam_counters' in ttypes
        return is_protobuf_zfits_file & is_nectarcam_file

    @staticmethod
    def fill_nectarcam_service_container_from_zfile(tel_id, camera_config):
        """
        Fill NectarCAM Service container with specific NectarCAM service data data
        (from the CameraConfig table of zfit file)
        """
        return NectarCAMServiceContainer(
            telescope_id=tel_id,
            cs_serial=camera_config.cs_serial,
            configuration_id=camera_config.configuration_id,
            acquisition_mode=camera_config.nectarcam.acquisition_mode,
            date=camera_config.date,
            num_pixels=camera_config.num_pixels,
            num_samples=camera_config.num_samples,
            pixel_ids=camera_config.expected_pixels_id,
            data_model_version=camera_config.data_model_version,
            num_modules=camera_config.nectarcam.num_modules,
            module_ids=camera_config.nectarcam.expected_modules_id,
            idaq_version=camera_config.nectarcam.idaq_version,
            cdhs_version=camera_config.nectarcam.cdhs_version,
            algorithms=camera_config.nectarcam.algorithms,
        )

    def fill_nectarcam_event_container_from_zfile(self, array_event, event):

        tel_id = self.tel_id
        event_container = NectarCAMEventContainer()
        array_event.nectarcam.tel[tel_id].evt = event_container

        event_container.configuration_id = event.configuration_id
        event_container.event_id = event.event_id
        event_container.tel_event_id = event.tel_event_id
        event_container.pixel_status = event.pixel_status
        event_container.ped_id = event.ped_id
        event_container.module_status = event.nectarcam.module_status
        event_container.extdevices_presence = event.nectarcam.extdevices_presence
        event_container.swat_data = event.nectarcam.swat_data
        event_container.counters = event.nectarcam.counters

        # unpack TIB data
        unpacked_tib = event.nectarcam.tib_data.view(TIB_DTYPE)[0]
        event_container.tib_event_counter = unpacked_tib[0]
        event_container.tib_pps_counter = unpacked_tib[1]
        event_container.tib_tenMHz_counter = unpacked_tib[2]
        event_container.tib_stereo_pattern = unpacked_tib[3]
        event_container.tib_masked_trigger = unpacked_tib[4]

        # unpack CDTS data
        is_old_cdts = len(event.nectarcam.cdts_data) < 36
        if is_old_cdts:
            unpacked_cdts = event.nectarcam.cdts_data.view(CDTS_BEFORE_37201_DTYPE)[0]
            event_container.ucts_event_counter = unpacked_cdts[0]
            event_container.ucts_pps_counter = unpacked_cdts[1]
            event_container.ucts_clock_counter = unpacked_cdts[2]
            event_container.ucts_timestamp = unpacked_cdts[3]
            event_container.ucts_camera_timestamp = unpacked_cdts[4]
            event_container.ucts_trigger_type = unpacked_cdts[5]
            event_container.ucts_white_rabbit_status = unpacked_cdts[6]
        else:
            unpacked_cdts = event.nectarcam.cdts_data.view(CDTS_AFTER_37201_DTYPE)[0]
            event_container.ucts_timestamp = unpacked_cdts[0]
            event_container.ucts_address = unpacked_cdts[1]  # new
            event_container.ucts_event_counter = unpacked_cdts[2]
            event_container.ucts_busy_counter = unpacked_cdts[3]  # new
            event_container.ucts_pps_counter = unpacked_cdts[4]
            event_container.ucts_clock_counter = unpacked_cdts[5]
            event_container.ucts_trigger_type = unpacked_cdts[6]
            event_container.ucts_white_rabbit_status = unpacked_cdts[7]
            event_container.ucts_stereo_pattern = unpacked_cdts[8]  # new
            event_container.ucts_num_in_bunch = unpacked_cdts[9]  # new
            event_container.cdts_version = unpacked_cdts[10]  # new

        # Unpack FEB counters and trigger pattern
        self.unpack_feb_data(event_container, event)

    def fill_trigger_info(self, array_event):
        tel_id = self.tel_id

        nectarcam = array_event.nectarcam.tel[tel_id]
        tib_available = nectarcam.evt.extdevices_presence & 1
        ucts_available = nectarcam.evt.extdevices_presence & 2

        # fill trigger time using UCTS timestamp
        trigger = array_event.trigger
        trigger_time = nectarcam.evt.ucts_timestamp
        trigger_time = time_from_unix_tai_ns(trigger_time)
        trigger.time = trigger_time
        trigger.tels_with_trigger = [tel_id]
        trigger.tel[tel_id].time = trigger.time

        # decide which source to use, if both are available,
        # the option decides, if not, fallback to the avilable source
        # if no source available, warn and do not fill trigger info
        if tib_available and ucts_available:
            if self.default_trigger_type == 'ucts':
                trigger_bits = nectarcam.evt.ucts_trigger_type
            else:
                trigger_bits = nectarcam.evt.tib_masked_trigger

        elif tib_available:
            trigger_bits = nectarcam.evt.tib_masked_trigger

        elif ucts_available:
            trigger_bits = nectarcam.evt.ucts_trigger_type

        else:
            self.log.warning('No trigger info available.')
            trigger.event_type = EventType.UNKNOWN
            return

        if (
                ucts_available
                and nectarcam.evt.ucts_trigger_type == 42  # TODO check if it's correct
                and self.default_trigger_type == "ucts"
        ):
            self.log.warning(
                'Event with UCTS trigger_type 42 found.'
                ' Probably means unreliable or shifted UCTS data.'
                ' Consider switching to TIB using `default_trigger_type="tib"`'
            )

        # first bit mono trigger, second stereo.
        # If *only* those two are set, we assume it's a physics event
        # for all other we only check if the flag is present
        if (trigger_bits & TriggerBits.PHYSICS) and not (trigger_bits & TriggerBits.OTHER):
            trigger.event_type = EventType.SUBARRAY
        elif trigger_bits & TriggerBits.CALIBRATION:
            trigger.event_type = EventType.FLATFIELD
        elif trigger_bits & TriggerBits.PEDESTAL:
            trigger.event_type = EventType.SKY_PEDESTAL
        elif trigger_bits & TriggerBits.SINGLE_PE:
            trigger.event_type = EventType.SINGLE_PE
        else:
            self.log.warning(
                f'Event {array_event.index.event_id} has unknown event type, trigger: {trigger_bits:08b}')
            trigger.event_type = EventType.UNKNOWN

    def unpack_feb_data(self, event_container, event):
        '''Unpack FEB counters and trigger pattern'''

        # Deduce data format version
        bytes_per_module = len(
            event.nectarcam.counters) // self.camera_config.nectarcam.num_modules
        # Remain compatible with data before addition of trigger pattern
        module_fmt = 'IHHIBBBBBBBB' if bytes_per_module > 16 else 'IHHIBBBB'
        n_fields = len(module_fmt)
        rec_fmt = '=' + module_fmt * self.camera_config.nectarcam.num_modules
        # Unpack
        unpacked_feb = struct.unpack(rec_fmt, event.nectarcam.counters)
        # Initialize field containers
        n_camera_modules = N_PIXELS // 7
        event_container.feb_abs_event_id = np.zeros(shape=(n_camera_modules,), dtype=np.uint32)
        event_container.feb_event_id = np.zeros(shape=(n_camera_modules,), dtype=np.uint16)
        event_container.feb_pps_cnt = np.zeros(shape=(n_camera_modules,), dtype=np.uint16)
        event_container.feb_ts1 = np.zeros(shape=(n_camera_modules,), dtype=np.uint32)
        event_container.feb_ts2_trig = np.zeros(shape=(n_camera_modules,), dtype=np.int16)
        event_container.feb_ts2_pps = np.zeros(shape=(n_camera_modules,), dtype=np.int16)
        if bytes_per_module > 16:
            n_patterns = 4
            event_container.trigger_pattern = np.zeros(shape=(n_patterns, N_PIXELS),
                                                       dtype=bool)

        # Unpack absolute event ID
        event_container.feb_abs_event_id[
            self.camera_config.nectarcam.expected_modules_id] = unpacked_feb[0::n_fields]
        # Unpack PPS counter
        event_container.feb_pps_cnt[
            self.camera_config.nectarcam.expected_modules_id] = unpacked_feb[1::n_fields]
        # Unpack relative event ID
        event_container.feb_event_id[
            self.camera_config.nectarcam.expected_modules_id] = unpacked_feb[2::n_fields]
        # Unpack TS1 counter
        event_container.feb_ts1[
            self.camera_config.nectarcam.expected_modules_id] = unpacked_feb[3::n_fields]
        # Unpack TS2 counters
        ts2_decimal = lambda bits: bits - (1 << 8) if bits & 0x80 != 0 else bits
        ts2_decimal_vec = np.vectorize(ts2_decimal)
        event_container.feb_ts2_trig[
            self.camera_config.nectarcam.expected_modules_id] = ts2_decimal_vec(
            unpacked_feb[4::n_fields])
        event_container.feb_ts2_pps[
            self.camera_config.nectarcam.expected_modules_id] = ts2_decimal_vec(
            unpacked_feb[5::n_fields])
        # Loop over modules
        for module_idx, module_id in enumerate(
                self.camera_config.nectarcam.expected_modules_id):
            offset = module_id * 7
            if bytes_per_module > 16:
                field_id = 8
                # Decode trigger pattern
                for pattern_id in range(n_patterns):
                    value = unpacked_feb[n_fields * module_idx + field_id + pattern_id]
                    module_pattern = [int(digit) for digit in
                                      reversed(bin(value)[2:].zfill(7))]
                    event_container.trigger_pattern[pattern_id,
                    offset:offset + 7] = module_pattern

        # Unpack native charge
        if len(event.nectarcam.charges_gain1) > 0:
            event_container.native_charge = np.zeros(shape=(N_GAINS, N_PIXELS),
                                                     dtype=np.uint16)
            rec_fmt = '=' + 'H' * self.camera_config.num_pixels
            for gain_id in range(N_GAINS):
                unpacked_charge = struct.unpack(rec_fmt, getattr(event.nectarcam,
                                                                 f'charges_gain{gain_id + 1}'))
                event_container.native_charge[
                    gain_id, self.camera_config.expected_pixels_id] = unpacked_charge

    def fill_r0r1_camera_container(self, zfits_event):
        """
        Fill the r0 or r1 container, depending on whether gain
        selection has already happened (r1) or not (r0)
        This will create waveforms of shape (N_GAINS, N_PIXELS, N_SAMPLES),
        or (N_PIXELS, N_SAMPLES) respectively regardless of the n_pixels, n_samples
        in the file.
        Missing or broken pixels are filled using maxval of the waveform dtype.
        """
        n_pixels = self.camera_config.num_pixels
        n_samples = self.camera_config.num_samples
        expected_pixels = self.camera_config.expected_pixels_id

        has_low_gain = (zfits_event.pixel_status & PixelStatus.LOW_GAIN_STORED).astype(bool)
        has_high_gain = (zfits_event.pixel_status & PixelStatus.HIGH_GAIN_STORED).astype(bool)
        not_broken = (has_low_gain | has_high_gain).astype(bool)

        # broken pixels have both false, so gain selected means checking
        # if there are any pixels where exactly one of high or low gain is stored
        gain_selected = np.any(has_low_gain != has_high_gain)

        # fill value for broken pixels
        dtype = zfits_event.waveform.dtype
        fill = np.iinfo(dtype).max
        # we assume that either all pixels are gain selected or none
        # only broken pixels are allowed to be missing completely
        if gain_selected:
            selected_gain = np.where(has_high_gain, 0, 1)
            waveform = np.full((n_pixels, n_samples), fill, dtype=dtype)
            waveform[not_broken] = zfits_event.waveform.reshape((-1, n_samples))

            reordered_waveform = np.full((N_PIXELS, N_SAMPLES), fill, dtype=dtype)
            reordered_waveform[expected_pixels] = waveform

            reordered_selected_gain = np.full(N_PIXELS, -1, dtype=np.int8)
            reordered_selected_gain[expected_pixels] = selected_gain

            r0 = R0CameraContainer()
            r1 = R1CameraContainer(
                waveform=reordered_waveform,
                selected_gain_channel=reordered_selected_gain,
            )
        else:
            reshaped_waveform = zfits_event.waveform.reshape(N_GAINS, n_pixels, n_samples)
            # re-order the waveform following the expected_pixels_id values
            #  could also just do waveform = reshaped_waveform[np.argsort(expected_ids)]
            reordered_waveform = np.full((N_GAINS, N_PIXELS, N_SAMPLES), fill, dtype=dtype)
            reordered_waveform[:, expected_pixels, :] = reshaped_waveform
            r0 = R0CameraContainer(waveform=reordered_waveform)
            r1 = R1CameraContainer()

        return r0, r1

    def fill_r0r1_container(self, array_event, zfits_event):
        """
        Fill with R0Container
        """
        r0, r1 = self.fill_r0r1_camera_container(zfits_event)
        array_event.r0.tel[self.tel_id] = r0
        array_event.r1.tel[self.tel_id] = r1

    def initialize_mon_container(self, array_event):
        """
        Fill with MonitoringContainer.
        For the moment, initialize only the PixelStatusContainer

        """
        container = array_event.mon
        mon_camera_container = container.tel[self.tel_id]

        # initialize the container
        status_container = PixelStatusContainer()

        shape = (N_GAINS, N_PIXELS)
        status_container.hardware_failing_pixels = np.zeros(shape, dtype=bool)
        status_container.pedestal_failing_pixels = np.zeros(shape, dtype=bool)
        status_container.flatfield_failing_pixels = np.zeros(shape, dtype=bool)

        mon_camera_container.pixel_status = status_container

    def fill_mon_container_from_zfile(self, array_event, event):
        """
        Fill with MonitoringContainer.
        For the moment, initialize only the PixelStatusContainer

        """

        status_container = array_event.mon.tel[self.tel_id].pixel_status

        # reorder the array
        pixel_status = np.zeros(N_PIXELS)
        pixel_status[self.camera_config.expected_pixels_id] = event.pixel_status
        status_container.hardware_failing_pixels[:] = pixel_status == 0

class MultiFiles:
    """
    This class open all the files in file_list and read the events following
    the event_id order
    """

    def __init__(self, file_list):

        self._file = {}
        self._events = {}
        self._events_table = {}
        self._camera_config = {}
        self.camera_config = None

        paths = []
        for file_name in file_list:
            paths.append(file_name)
            Provenance().add_input_file(file_name, role='r0.sub.evt')

        # open the files and get the first fits Tables
        from protozfits import File

        for path in paths:

            try:
                self._file[path] = File(str(path))
                self._events_table[path] = File(str(path)).Events
                self._events[path] = next(self._file[path].Events)

                # verify where the CameraConfig is present
                if 'CameraConfig' in self._file[path].__dict__.keys():
                    self._camera_config[path] = next(self._file[path].CameraConfig)

                    # for the moment it takes the first CameraConfig it finds (to be changed)
                    if (self.camera_config is None):
                        self.camera_config = self._camera_config[path]


            except StopIteration:
                pass

        # verify that somewhere the CameraConfing is present
        assert (self.camera_config)

    def __iter__(self):
        return self

    def __next__(self):
        return self.next_event()

    def next_event(self):
        # check for the minimal event id
        if not self._events:
            raise StopIteration

        min_path = min(
            self._events.items(),
            key=lambda item: item[1].event_id,
        )[0]

        # return the minimal event id
        next_event = self._events[min_path]
        try:
            self._events[min_path] = next(self._file[min_path].Events)
        except StopIteration:
            del self._events[min_path]

        return next_event

    def __len__(self):
        total_length = sum(
            len(table)
            for table in self._events_table.values()
        )
        return total_length

    def num_inputs(self):
        return len(self._file)
