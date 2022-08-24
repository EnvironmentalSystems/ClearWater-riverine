import h5py
import numpy as np
import pandas as pd
import xarray as xr
import numba
import datetime
from scipy.sparse import *
from scipy.sparse.linalg import *

### TODD FUNCTIONS

def parse_attributes(dataset):
    '''Parse the HDF5 attributes array, convert binary strings to Python strings, and return a dictionary of attributes'''
    attrs = {}
    for key, value in dataset.attrs.items():
        if type(value) == np.bytes_:
            attrs[key] = value.decode('ascii')
        elif type(value) == np.ndarray:
            values = []
            for v in value:
                if type(v) == np.bytes_:
                    values.append(v.decode('ascii'))
                else:
                    values.append(v)
            attrs[key] = values
        else:
            attrs[key] = value
    return attrs

def hdf_to_xarray(dataset, dims, attrs=None) -> xr.DataArray:
    '''Read n-dimensional HDF5 dataset and return it as an xarray.DataArray'''
    if attrs == None:
        attrs = parse_attributes(dataset)
    data_array = xr.DataArray(dataset[()], dims = dims, attrs = attrs)
    return data_array

def hdf_to_pandas(dataset) -> pd.DataFrame:
    '''Read n-dimensional HDF5 dataset and return it as an xarray.DataArray'''
    attrs = parse_attributes(dataset)
    df = pd.DataFrame(dataset[()], columns = attrs['Column'])
    return df

@numba.njit
def interp(x0: float, x1: float, y0: float, y1: float, xi: float):
    '''
    Linear interpolation:

    Inputs:
        x0: Lower x value
        x1: Upper x value
        y0: Lower y value
        y1: Upper y value
        xi: x value to interpolate

    Returns:
        y1: interpolated y value
    '''
    m = (y1 - y0)/(x1 - x0)
    yi = m * (xi - x0) + y0
    return yi

@numba.njit
def compute_cell_volumes(water_surface_elev_arr: np.ndarray, cells_surface_area_arr: np.ndarray, starting_index_arr: np.ndarray, count_arr: np.ndarray, elev_arr: np.ndarray, vol_arr: np.ndarray, VERBOSE=False) -> float:
    '''Compute the volumes of the RAS cells using lookup tables'''
    ntimes, ncells = water_surface_elev_arr.shape
    cell_volumes = np.zeros((ntimes, ncells))

    for time in range(ntimes):
        for cell in range(ncells):
            water_surface_elev = water_surface_elev_arr[time, cell]
            surface_area = cells_surface_area_arr[cell]
            index = starting_index_arr[cell] # Start index in the volume-elevation table for this cell
            count = count_arr[cell] # Number of points in the table for this cell

            # A number of cells have an index that is just past the end of the array. According to Mark Jensen, 
            # these are ghost cells and have a volume of 0.0. The count for these cells should also always be zero. 
            # The code checks for either condition.
            if index >= len(elev_arr) or count == 0:
                cell_volumes[time, cell] = 0.0
            else:
                elev = elev_arr[index:index + count] # Get the water surface elevation array for this cell
                vol = vol_arr[index:index + count] # Get the volume array for this cell

                if water_surface_elev > elev[-1]:
                    '''
                    Compute the net volume: the max volume in the lookup table plus the volume of the water above the max 
                    elevation in the lookup table.
                    
                    Note: this assumes a horizontal water surface, i.e., that the slope of the water surface across a cell
                    is negligible. The error increases with cell size.
                    
                    The validity of this method was confirmed by Mark Jensen on Jul 29, 2022.
                    '''
                    cell_volumes[time, cell] = vol[-1] + (water_surface_elev - elev[-1]) * surface_area
                elif water_surface_elev == elev[-1]:
                    cell_volumes[time, cell] = vol[-1]
                elif water_surface_elev <= elev[0]:
                    cell_volumes[time, cell] = vol[0]
                else:
                    # Interpolate
                    cell_volumes[time, cell] = 0.0 # Default
                    npts = len(elev)
                    for i in range(npts-1, -1, -1):
                        if elev[i] < water_surface_elev:
                            cell_volumes[time, cell] = interp(elev[i], elev[i+1], vol[i], vol[i+1], water_surface_elev)

    return cell_volumes

@numba.njit
def compute_face_areas(water_surface_elev_arr: np.ndarray, faces_lengths_arr: np.ndarray, faces_cell_indexes_arr: np.ndarray, starting_index_arr: np.ndarray, count_arr: np.ndarray, elev_arr: np.ndarray, area_arr: np.ndarray):
    '''Compute the areas of the RAS cell faces using lookup tables'''
    ntimes, ncells = water_surface_elev_arr.shape
    nfaces = len(faces_lengths_arr)
    face_areas = np.zeros((ntimes, nfaces))
    for time in range(ntimes):
        for face in range(nfaces):
            cell = faces_cell_indexes_arr[face]
            water_surface_elev = water_surface_elev_arr[time, cell]
            index = starting_index_arr[face] # Start index in the area-elevation table for this cell (note: this is indexed by faces)
            count = count_arr[face] # Number of points in the table for this cell (note: this is indexed by faces)

            # A number of cells have an index that is just past the end of the array. According to Mark Jensen, 
            # these are ghost cells and have a volume of 0.0. The count for these cells should also always be zero. 
            # The code checks for either condition.
            if index >= len(elev_arr) or count == 0:
                face_areas[time, face] = 0.0
            else:
                elev = elev_arr[index:index + count] # Get the water surface elevation (Z) array for this face
                area = area_arr[index:index + count] # Get the face area array for this face

                if water_surface_elev > elev[-1]:
                    '''
                    Compute the net face surface area: the max face area in the lookup table plus the face area of 
                    the water above the max elevation in the lookup table.
                    
                    Note: this assumes a horizontal water surface, i.e., that the slope of the water surface across a cell face
                    is negligible. The error increases with cell size.
                    
                    The validity of this method was confirmed by Mark Jensen on Jul 29, 2022.
                    '''
                    face_areas[time, face] = area[-1] + (water_surface_elev - elev[-1]) * faces_lengths_arr[face]
                elif water_surface_elev == elev[-1]:
                    face_areas[time, face] = area[-1]
                elif water_surface_elev <= elev[0]:
                    face_areas[time, face] = area[0]
                else:
                    # Interpolate
                    face_areas[time, face] = 0.0 # Default
                    npts = len(elev)
                    for i in range(npts-1, -1, -1):
                        if elev[i] < water_surface_elev:
                            x = water_surface_elev
                            face_areas[time, face] = interp(elev[i], elev[i+1], area[i], area[i+1], water_surface_elev)
                            # print('i, x, m, x1, y1, y: ', i, x, m, x1, y1, y)
                            if face_areas[time, face] < 0:
                                print('Computed face area = ', face_areas[time, face])
                                print('Time Step: ', time)
                                print('Cell number: ', cell)
                                print('Face number: ', face)
                                print('water_surface_elev: ', water_surface_elev)
                                print('elev: ', elev)
                                print('area: ', area)
                                # msg = 'Negative face area: computed face area = ' + str(face_areas[time, face])
                                raise(ValueError('Negative face area'))

    return face_areas

### END OF TODD's CODE 


def parse_project_name(infile: h5py._hl.files.File) -> str:
    '''Parse the name of a project'''
    project_name = infile['Geometry/2D Flow Areas/Attributes'][()][0][0].decode('UTF-8')
    return project_name

def calc_distances_cell_centroids(mesh: xr.Dataset) -> np.array:
    # define the faces (cells) on either side of an edge 
    # f1 is the first cell (start) and f2 is the second cell (end)
    f1 = mesh['edge_face_connectivity'].T[0]
    f2 = mesh['edge_face_connectivity'].T[1]

    # get northings/eastings of relevant faces 
    x1_coords = mesh['face_x'][f1]
    y1_coords = mesh['face_y'][f1]
    x2_coords = mesh['face_x'][f2]
    y2_coords = mesh['face_y'][f2]

    # calculate distance 
    dist_data = np.sqrt((x1_coords - x2_coords)**2 + (y1_coords - y2_coords)**2)
    return dist_data

def calc_coeff_to_diffusion_term(mesh: xr.Dataset, diffusion_coefficient_input: float) -> np.array:
    # diffusion coefficient: ignore diffusion between cells in the mesh and ghost cells
    diffusion_coefficient = np.zeros(len(mesh['nedge']))

    # identify ghost cells: 
    # ghost cells are only in the second element of a pair or cell indices that denote an edge together
    f1 = mesh['edge_face_connectivity'].T[0]
    f2 = mesh['edge_face_connectivity'].T[1]

    # number of real cells (cell 1 in a pair of cells that define an edge is always a real cell)
    nreal = f1.max()

    f1_ghost = np.where(f1 <= nreal) 
    f2_ghost = np.where(f2 <= nreal)

    # set diffusion coefficients where NOT pseusdo cell 
    diffusion_coefficient[np.array(f2_ghost)] = diffusion_coefficient_input

    # diffusion_array =  mesh['edge_vertical_area'] * diffusion_coefficient / mesh['face_to_face_dist']
    diffusion_array =  mesh['edge_vertical_area'] * diffusion_coefficient_input / mesh['face_to_face_dist']
    return diffusion_array

def sum_vals(mesh: xr.Dataset, face: np.array, time_index: float, sum_array: np.array) -> np.array:
    '''
    https://stackoverflow.com/questions/67108215/how-to-get-sum-of-values-in-a-numpy-array-based-on-another-array-with-repetitive
    '''
    # _, idx, _ = np.unique(face, return_counts=True, return_inverse=True)
    nodal_values = np.bincount(face.values, mesh['coeff_to_diffusion'][time_index])
    sum_array[0:len(nodal_values)] = nodal_values
    return sum_array

def calc_sum_coeff_to_diffusion_term(mesh: xr.Dataset) -> np.array:
    # initialize array
    sum_diffusion_array = np.zeros((len(mesh['time']), len(mesh['nface'])))

    # FACE 1 
    for t in range(len(mesh['time'])):
        # initialize arrays
        f1_sums = np.zeros(len(mesh['nface'])) 
        f2_sums = np.zeros(len(mesh['nface']))

        # faces - maybe kick this out + input to all these functions ?
        f1 = mesh['edge_face_connectivity'].T[0]
        f2 = mesh['edge_face_connectivity'].T[1]

        # calculate sums for all values
        f1_sums = sum_vals(mesh, f1, t, f1_sums)
        f2_sums = sum_vals(mesh, f2, t, f2_sums)

        # add f1_sums and f2_sums together to get total 
        # need to do this because sometimes a cell is f1 (the first cell in a pair defining an edge)
        # and osmetimes a cell is f2 (the second cell in a pair defining an edge)
        sum_diffusion_array[t] = f1_sums + f2_sums
    return sum_diffusion_array

def calc_ghost_cell_volumes(mesh: xr.Dataset) -> np.array:
    # faces - maybe kick this out + input to all these functions ?
    f1 = mesh['edge_face_connectivity'].T[0]
    f2 = mesh['edge_face_connectivity'].T[1]

    # find ghost cells receiving flow  - kick this out too?
    nreal = f1.max()
    f2_ghost = np.where(f2 > nreal)[0]  

    ghost_vels = np.zeros((len(mesh['time']), len(mesh['nedge'])))

    for t in range(len(mesh['time'])):
        # positive velocities 
        positive_velocity_indices = np.where(mesh['edge_velocity'][t] > 0 )[0]

        # get intersection - this is where water is flowing OUT to a ghost cell
        index_list = np.intersect1d(positive_velocity_indices, f2_ghost)

        if len(index_list) == 0:
            pass
        else:
            ghost_vels[t][index_list] = mesh['edge_velocity'][t][index_list]

    # calculate volume
    ghost_flux_vols = ghost_vels * mesh['edge_vertical_area'] * mesh['dt']
    
    # transfer values (acssociated with EDGES) to corresponding CELLS (FACES)
    ghost_vols = np.zeros((len(mesh['time']), len(mesh['nface'])))
    for t in range(len(mesh['time'])):
        indices = np.where(ghost_flux_vols[t] > 0)[0]
        cell_ind = f2[indices]
        vals = ghost_flux_vols[t][indices]
        if len(cell_ind) > 0:
            ghost_vols[t][np.array(cell_ind)] = vals 
        else:
            pass

    return ghost_vols


def define_ugrid(infile: h5py._hl.files.File, project_name: str) -> xr.Dataset:
    '''Define UGRID-compliant xarray'''

    # initialize mesh
    mesh = xr.Dataset()

    # initialize topology
    mesh["mesh2d"] = xr.DataArray(
        data=0,
        attrs={
            # required topology attributes
            'cf_role': 'mesh_topology',
            'long_name': 'Topology data of 2D mesh',
            'topology_dimension': 2,
            'node_coordinates': 'node_x node_y',
            'face_node_connectivity': 'face_nodes',
            # optionally required attributes
            'face_dimension': 'face',
            'edge_node_connectivity': 'edge_nodes',
            'edge_dimension': 'edge',
            # optional attributes 
            'face_edge_connectivity': 'face_edges',
            'face_face_connectivity': 'face_face_connectivity',
            'edge_face_connectivity': 'edge_face_connectivity',
            'boundary_node_connectivity': 'boundary_node_connectivity',
            'face_coordinates': 'face x face_y',
            'edge_coordinates': 'edge_x edge_y',
            },
        )
    # assign coordinates 
    # x-coordinates
    mesh = mesh.assign_coords(
        node_x=xr.DataArray(
            data = infile[f'Geometry/2D Flow Areas/{project_name}/Cells FacePoint Indexes'][()].T[0],
            # data=[f[0] for f in ras2d_data.geometry['nodes_array']],
            dims=("node",),
            )   
        )
    # y-coordinates
    mesh = mesh.assign_coords(
            node_y=xr.DataArray(
            data=infile[f'Geometry/2D Flow Areas/{project_name}/Cells FacePoint Indexes'][()].T[1],
            dims=("node",),
        )
    )
    # time
    time_stamps_binary = infile['Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/Time Date Stamp'][()]
    time_stamps = [x.decode("utf8") for x in time_stamps_binary]
    mesh = mesh.assign_coords(
            time=xr.DataArray(
            data=[datetime.datetime.strptime(x, '%d%b%Y %H:%M:%S') for x in time_stamps], # '02JAN1900 22:55:00'
            dims=("time",),
                )
        )
        
    # define topology
    # face nodes 
    mesh["face_nodes"] = xr.DataArray(
        data=infile[f'Geometry/2D Flow Areas/{project_name}/Cells FacePoint Indexes'][()],
        coords={
            "face_x": ("nface", infile[f'Geometry/2D Flow Areas/{project_name}/Cells Center Coordinate'][()].T[0]),
            "face_y": ("nface", infile[f'Geometry/2D Flow Areas/{project_name}/Cells Center Coordinate'][()].T[1]),
        },
        dims=("nface", "nmax_face"),
        attrs={
            'cf_role': 'face_node_connectivity',
            'long_name': 'Vertex nodes of mesh faces (counterclockwise)',
            'start_index': 0, 
            '_FillValue': -1
    })
    # edge nodes 
    mesh["edge_nodes"] = xr.DataArray(
        data=infile[f'Geometry/2D Flow Areas/{project_name}/Faces FacePoint Indexes'][()],
        dims=("nedge", '2'),
        attrs={
            'cf_role': 'edge_node_connectivity',
            'long_name': 'Vertex nodes of mesh edges',
            'start_index': 0
        })
    # edge face connectivity
    mesh["edge_face_connectivity"] = xr.DataArray(
        data=infile[f'Geometry/2D Flow Areas/{project_name}/Faces Cell Indexes'][()],
        dims=("nedge", '2'),
        attrs={
            'cf_role': 'edge_face_connectivity',
            'long_name': 'neighbor faces for edges',
            'start_index': 0
        })
        
    return mesh



def populate_ugrid(infile: h5py._hl.files.File, project_name: str, diffusion_coefficient_input: float) -> xr.Dataset:
    # pre-computed values
    mesh = define_ugrid(infile, project_name)

    # surface area 
    mesh['faces_surface_area'] = hdf_to_xarray(infile[f'Geometry/2D Flow Areas/{project_name}/Cells Surface Area'],
                                                ("nface"))
    # edge velocity
    mesh['edge_velocity'] = hdf_to_xarray(infile[f'Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/{project_name}/Face Velocity'], 
                                           ('time', 'nedge'))
    # edge length
    mesh['edge_length'] = hdf_to_xarray(infile[f'Geometry/2D Flow Areas/{project_name}/Faces NormalUnitVector and Length'][:,2],
                                        ("nedge"), attrs={'Units': 'ft'}) # to do : parse from HDF 
    # water surface elev 
    mesh['water_surface_elev'] = hdf_to_xarray(infile[f'Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/{project_name}/Water Surface'], 
                                                (['time', 'nface']))
    # compute necessary values 
    # TO DO: clean all this up; review functions to see if they can be simplified 
    # calculate cell volume
    cells_volume_elevation_info_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Cells Volume Elevation Info'])
    cells_volume_elevation_values_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Cells Volume Elevation Values'])
    # question: is it better to separate all of these things
    # or just input mesh, cells_volume_elevation_info_df / values df 
    cell_volumes = compute_cell_volumes(
                                        mesh['water_surface_elev'].values,
                                        mesh['faces_surface_area'].values,
                                        cells_volume_elevation_info_df['Starting Index'].values,
                                        cells_volume_elevation_info_df['Count'].values,
                                        cells_volume_elevation_values_df['Elevation'].values,
                                        cells_volume_elevation_values_df['Volume'].values,
                                            )
    mesh['volume'] = hdf_to_xarray(cell_volumes, ('time', 'nface'), attrs={'Units': 'ft3'}) 

    # calculate edge vertical area 
    faces_area_elevation_info_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Faces Area Elevation Info'])
    faces_area_elevation_values_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Faces Area Elevation Values'])
    faces_normalunitvector_and_length_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Faces NormalUnitVector and Length'])
    faces_cell_indexes_df = hdf_to_pandas(infile[f'Geometry/2D Flow Areas/{project_name}/Faces Cell Indexes'])
    # should we be using 0 or 1 ?
    face_areas_0 = compute_face_areas(
                                        mesh['water_surface_elev'].values,
                                        faces_normalunitvector_and_length_df['Face Length'].values,
                                        faces_cell_indexes_df['Cell 0'].values,
                                        faces_area_elevation_info_df['Starting Index'].values,
                                        faces_area_elevation_info_df['Count'].values,
                                        faces_area_elevation_values_df['Z'].values,
                                        faces_area_elevation_values_df['Area'].values,
                                    )
    mesh['edge_vertical_area'] = hdf_to_xarray(face_areas_0, ('time', 'nedge'), attrs={'Units': 'ft'})
    

    # computed values 
    # distance between centroids 
    distances = calc_distances_cell_centroids(mesh)
    mesh['face_to_face_dist'] = hdf_to_xarray(distances, ('nedge'), attrs={'Units': 'ft'})

    # coefficient to diffusion term
    coeff_to_diffusion = calc_coeff_to_diffusion_term(mesh, diffusion_coefficient_input)
    mesh['coeff_to_diffusion'] = hdf_to_xarray(coeff_to_diffusion, ("time", "nedge"), attrs={'Units': 'ft3/s'})

    # sum of diffusion coeff
    sum_coeff_to_diffusion = calc_sum_coeff_to_diffusion_term(mesh)
    mesh['sum_coeff_to_diffusion'] = hdf_to_xarray(sum_coeff_to_diffusion, ('time', 'nface'), attrs={'Units':'ft3/s'})

    # advection
    advection_coefficient = mesh['edge_vertical_area'] * mesh['edge_velocity'] 
    mesh['advection_coeff'] = hdf_to_xarray(advection_coefficient, ('time', 'nedge'), attrs={'Units':'ft3/s'})

    # dt
    dt = np.ediff1d(mesh['time'])
    dt = dt / np.timedelta64(1, 's')
    dt = np.insert(dt, len(dt), np.nan)
    mesh['dt'] = hdf_to_xarray(dt, ('time'), attrs={'Units': 's'})

    # ghost cell volumes
    ghost_volumes = calc_ghost_cell_volumes(mesh)
    mesh['ghost_volumes'] = hdf_to_xarray(ghost_volumes, ('time', 'nface'), attrs={'Units':'ft3'})
    
    return mesh

# matrix solver 
class LHS:
    def __init__(self, mesh: xr.Dataset, t: float):
        '''
        mesh: xarray dataset containing all geometry and ouptut results from RAS2D.
            Should follow UGRID conventions.
        params: A class instance containing additional parameters. 
            TBD if this will remain or if parameters will be integrated into xarray.
        t: timestep index  
        '''
        return
        
    def updateValues(self, mesh: xr.Dataset,  t:float):
        flow_out_indices = np.where(mesh['advection_coeff'][t+1] > 0)[0]
        flow_in_indices = np.where(mesh['advection_coeff'][t+1] < 0)[0]

        # initialize arrays that will define the sparse matrix 
        len_val = len(mesh['nedge']) * 2 + len(mesh['nface']) * 2 + len(flow_out_indices)* 2  + len(flow_in_indices)*2
        self.rows = np.zeros(len_val)
        self.cols = np.zeros(len_val)
        self.coef = np.zeros(len_val)

        ###### diagonal terms - the "A" coefficient in the equations detailed above. 
        start = 0
        end = len(mesh['nface'])
        self.rows[start:end] = mesh['nface']
        self.cols[start:end] = mesh['nface']
        seconds = mesh['dt'].values[t] # / np.timedelta64(1, 's'))
        self.coef[start:end] = mesh['volume'][t+1] / seconds + mesh['sum_coeff_to_diffusion'][t+1] # maybe rename in final code: diffusion term coeff

        # add ghost cell volumes tp diagonals
        # note: these values are 0 for cell that is NOT a ghost cell
        # note: also 0 for any ghost cell that is not RECEIVING flow 
        start = end
        end = end + len(mesh['nface'])
        self.rows[start:end] = mesh['nface']
        self.cols[start:end] = mesh['nface']
        self.coef[start:end] = mesh['ghost_volumes'][t+1] / seconds 
             
        ###### advection
        # if statement to prevent errors if flow_out_indices or flow_in_indices have length of 0
        if len(flow_out_indices) > 0:
            # update indices
            start = end
            end = end + len(flow_out_indices)

            # where advection coefficient is positive, the concentration across the face will be the REFERENCE CELL 
            # so the the coefficient will go in the diagonal - both row and column will equal diag_cell
            self.rows[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.coef[start:end] = mesh['advection_coeff'][t+1][flow_out_indices]  

            # subtract from corresponding neighbor cell (off-diagonal)
            start = end
            end = end + len(flow_out_indices)
            self.rows[start:end] = mesh['edge_face_connectivity'].T[1][flow_out_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.coef[start:end] = mesh['advection_coeff'][t+1][flow_out_indices] * -1  
        else:
            pass

        if len(flow_in_indices) > 0:
            # update indices
            start = end
            end = end + len(flow_in_indices)

            ## where it is negative, the concentration across the face will be the neighbor cell ("N")
            ## so the coefficient will be off-diagonal 
            self.rows[start:end] = mesh['edge_face_connectivity'].T[0][flow_in_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.coef[start:end] = mesh['advection_coeff'][t+1][flow_in_indices] 

            ## update indices 
            start = end
            end = end + len(flow_in_indices)
            ## do the opposite on the corresponding diagonal 
            self.rows[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.coef[start:end] = mesh['advection_coeff'][t+1][flow_in_indices]  * -1 
        else:
            pass
        
        ###### off-diagonal terms - diffusion
        f1 = mesh['edge_face_connectivity'].T[0]
        f2 = mesh['edge_face_connectivity'].T[1]

        # update indices
        start = end
        end = end + len(mesh['nedge'])
        self.rows[start:end] = f1
        self.cols[start:end] = f2
        self.coef[start:end] = -1 * mesh['coeff_to_diffusion'][t+1]

        # update indices and repeat 
        start = end
        end = end + len(mesh['nedge'])
        self.cols[start:end] = f1
        self.rows[start:end] = f2
        self.coef[start:end] = -1 * mesh['coeff_to_diffusion'][t+1] # last one
        return

class RHS:
    def __init__(self, mesh: xr.Dataset, t: float):
        '''
        mesh: xarray dataset containing all geometry and ouptut results from RAS2D.
            Should follow UGRID conventions.
        params: A class instance containing additional parameters. 
            TBD if this will remain or if parameters will be integrated into xarray.
        t: timestep index  
        '''
        self.conc = np.zeros(len(mesh['nface']))
        self.conc[0] = 5000 # put a concentration in the top left cell to start: tweak this to take initial
        self.vals = np.zeros(len(mesh['nface']))
        seconds = mesh['dt'].values[t] # / np.timedelta64(1, 's'))
        self.vals[:] = mesh['volume'][t] / seconds * self.conc 
        return 
    def updateValues(self, vector, ds, t):
        seconds = ds['dt'].values[t] # / np.timedelta64(1, 's'))
        self.vals[:] = vector * ds['volume'][t] / seconds
        return


def wq_simulation(mesh: xr.Dataset) -> xr.Dataset:
    t = 0
    b = RHS(mesh, t)
    output = np.zeros((len(mesh['time']), len(mesh['nface'])))
    for t in range(len(mesh['time']) - 1):
        output[t] = b.vals
        lhs = LHS(mesh, t)
        lhs.updateValues(mesh, t)
        A = csr_matrix( (lhs.coef,(lhs.rows, lhs.cols)), shape=(len(mesh['nface']),len(mesh['nface'])))
        x = spsolve(A, b.vals)
        b.updateValues(x, mesh, t+1)
    output[len(mesh['time']) - 1][:] = np.nan
    mesh['load'] = hdf_to_xarray(output, dims=('time', 'nface'), attrs={'Units': 'ft3'}) # check units 
    return mesh


def main(fpath: str, diffusion_coefficient: float):
    # define project name 
    with h5py.File(fpath, 'r') as infile:
        project_name = parse_project_name(infile)
        mesh = populate_ugrid(infile, project_name, diffusion_coefficient)
    return mesh


