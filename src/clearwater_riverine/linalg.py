import numpy as np
import xarray as xr

from clearwater_riverine import variables

# matrix solver 
class LHS:
    def __init__(self, mesh: xr.Dataset):
        """ Initialize Sparse Matrix used to solve transport equation. 

        Rather than looping through every single cell at every timestep, we can instead set up a sparse 
        matrix at each timestep that will allow us to solve the entire unstructured grid all at once. 
        We will solve an implicit Advection-Diffusion (transport) equation for the fractional total-load 
        concentrations. This discretization produces a linear system of equations that can be represented by 
        a sparse-matrix problem. 

        """
        self.internal_edges = np.where((mesh[variables.EDGES_FACE1] <= mesh.nreal) & (mesh[variables.EDGES_FACE2] <= mesh.nreal))[0]
        self.internal_edge_count = len(self.internal_edges)
        self.nreal_count = mesh.nreal + 1 # 0 indexed
                
    def update_values(self, mesh: xr.Dataset, t: float):
        """ Updates values in the LHS matrix based on the timestep. 

        A sparse matrix is a matrix that is mostly zeroes. Here, we will set up an NCELL x NCELL sparse matrix. 
            - The diagonal values represent the reference cell ("P")
            - The non-zero off-diagonal values represent the other cells that share an edge with that cell:
                i.e., neighboring cell ("N") that shares a face ("f") with P. 

        This function populates the sparse matrix with:
            - Values on the Diagonal (associated with the cell with the same index as that row/column):
                - Load at the t+1 timestep (volume at the t + 1 timestep / change in time)
                - Sum of diffusion coefficients associated with a cell
                - FOR DRY CELLS ONLY (volume = 0), insert a dummy value (1) so that the matrix is not singular
            - Values Off-Diagonal:
                - Coefficient to the diffusion term at the t+1 timestep 
            - Advection: a special case (updwinds scheme)
                - When the advection coefficient is positive, the concentration across the face will be the reference cell ("P")
                    so the coefficient will go in the diagonal. This value will then be subtracted from the corresponding neighbor cell.
                - When the advection coefficient is negative, the concentration across the face will be the neighbor cell ("N")
                    so the coefficient will be off-diagonal. This value will the subtracted from the corresponding reference cell.

        Attributes:
            rows / cols: point to the row and column of each cell
            coef: value in the specified row, column pair in the matrix 
        """
        # define edges where flow is flowing in versus out and find all empty cells
        # at the t+1 timestep
        flow_out_indices = np.where(mesh[variables.ADVECTION_COEFFICIENT][t+1] > 0)[0][0:self.internal_edge_count]
        flow_in_indices = np.where(mesh[variables.ADVECTION_COEFFICIENT][t+1] < 0)[0][0:self.internal_edge_count]
        empty_cells = np.where(mesh[variables.VOLUME][t+1] == 0)[0][0:self.nreal_count]

        # initialize arrays that will define the sparse matrix 
        len_val = self.internal_edge_count * 2 + self.nreal_count * 2 + len(flow_out_indices)* 2  + len(flow_in_indices)*2 + len(empty_cells)
        self.rows = np.zeros(len_val)
        self.cols = np.zeros(len_val)
        self.coef = np.zeros(len_val)

        # put dummy values in dry cells
        start = 0
        end = len(empty_cells)
        self.rows[start:end] = empty_cells
        self.cols[start:end] = empty_cells
        self.coef[start:end] = 1

        ###### diagonal terms - load and sum of diffusion coefficients associated with each cell
        start = end
        end = end + self.nreal_count
        self.rows[start:end] = mesh['nface'][0:self.nreal_count]
        self.cols[start:end] = mesh['nface'][0:self.nreal_count]
        seconds = mesh[variables.CHANGE_IN_TIME].values[t] # / np.timedelta64(1, 's'))
        self.coef[start:end] = mesh[variables.VOLUME][t+1][0:self.nreal_count] / seconds + mesh[variables.SUM_OF_COEFFICIENTS_TO_DIFFUSION_TERM][t+1][0:self.nreal_count]

        # # add ghost cell volumes to diagonals: based on flow across face into ghost cell
        # # note: these values are 0 for cell that is not a ghost cell
        # # note: also 0 for any ghost cell that is not RECEIVING flow 
        # start = end
        # end = end + self.nreal_count
        # self.rows[start:end] = mesh['nface']
        # self.cols[start:end] = mesh['nface']
        # self.coef[start:end] = mesh[variables.GHOST_CELL_VOLUMES_OUT][t+1] / seconds
             
        ###### advection
        # if statement to prevent errors if flow_out_indices or flow_in_indices have length of 0
        if len(flow_out_indices) > 0:
            start = end
            end = end + len(flow_out_indices)

            # where advection coefficient is positive, the concentration across the face will be the REFERENCE CELL 
            # so the the coefficient will go in the diagonal - both row and column will equal diag_cell
            self.rows[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.coef[start:end] = mesh[variables.ADVECTION_COEFFICIENT][t+1][flow_out_indices]  

            # subtract from corresponding neighbor cell (off-diagonal)
            start = end
            end = end + len(flow_out_indices)
            self.rows[start:end] = mesh['edge_face_connectivity'].T[1][flow_out_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[0][flow_out_indices]
            self.coef[start:end] = mesh[variables.ADVECTION_COEFFICIENT][t+1][flow_out_indices] * -1  

        if len(flow_in_indices) > 0:
            # update indices
            start = end
            end = end + len(flow_in_indices)

            ## where it is negative, the concentration across the face will be the neighbor cell ("N")
            ## so the coefficient will be off-diagonal 
            self.rows[start:end] = mesh['edge_face_connectivity'].T[0][flow_in_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.coef[start:end] = mesh[variables.ADVECTION_COEFFICIENT][t+1][flow_in_indices] 

            ## update indices 
            start = end
            end = end + len(flow_in_indices)
            ## do the opposite on the corresponding diagonal 
            self.rows[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.cols[start:end] = mesh['edge_face_connectivity'].T[1][flow_in_indices]
            self.coef[start:end] = mesh[variables.ADVECTION_COEFFICIENT][t+1][flow_in_indices]  * -1 
        
        ###### off-diagonal terms - diffusion
        # update indices
        start = end
        end = end + self.internal_edge_count
        self.rows[start:end] = mesh['edges_face1'][self.internal_edges]
        self.cols[start:end] = mesh['edges_face2'][self.internal_edges]
        self.coef[start:end] = -1 * mesh[variables.COEFFICIENT_TO_DIFFUSION_TERM][t+1][self.internal_edges]

        # update indices and repeat 
        start = end
        end = end + len(mesh['nedge'])
        self.rows[start:end] = mesh['edges_face2'][self.internal_edges]
        self.cols[start:end] = mesh['edges_face1'][self.internal_edges]
        self.coef[start:end] = -1 * mesh[variables.COEFFICIENT_TO_DIFFUSION_TERM][t+1][self.internal_edges]

class RHS:
    def __init__(self, mesh: xr.Dataset, t: int, inp: np.array):
        """
        Initialize the right-hand side matrix of concentrations based on user-defined boundary conditions. 

        Args:
            mesh (xr.Dataset):   UGRID-complaint xarray Dataset with all data required for the transport equation.
            t (int):             Timestep
            inp (np.array):      Array of shape (time x nface) with user-defined inputs of concentrations
                                    in each cell at each timestep. 

        Notes:
            Need to consider how ghost volumes / cells will be handled. 
            Need to consider how we will format the user-defined inputs 
                - An Excel file?
                - A modifiable table in a Jupyter notebook?
                - Alternatives?
        """
        self.conc = np.zeros(len(mesh['nface']))
        self.conc = inp[t][0:mesh.attrs.nreal] 
        self.vals = np.zeros(mesh.attrs.nreal)
        self.ghost_cells = np.where(mesh[variables.EDGES_FACE2] > mesh.attrs.nreal)[0]

        # seconds = mesh[variables.CHANGE_IN_TIME].values[t]
        # # SHOULD GHOST VOLUMES BE INCLUDED?
        # vol = mesh[variables.VOLUME][t][0:mesh.attrs.nreal] # + mesh[variables.GHOST_CELL_VOLUMES_IN][t] # + mesh[variables.GHOST_CELL_VOLUMES_OUT][t]
        self.vals[:] = self._calculate_rhs(self, mesh, t, self.conc)

    def update_values(self, solution: np.array, mesh: xr.Dataset, t: int, inp: np.array):
        """ 
        Update right hand side data based on the solution from the previous timestep
            solution: solution from solving the sparse matrix 
            inp: array of shape (time x nface) with user defined inputs of concentrations
                in each cell at each timestep 

        Args:
            solution (np.array):    Solution of concentrations at timestep t from solving sparse matrix. 
            mesh (xr.Dataset):      UGRID-complaint xarray Dataset with all data required for the transport equation.
            t (int):                Timestep
            inp (np.array):         Array of shape (time x nface) with user-defined inputs of concentrations
                                        in each cell at each timestep [boundary conditions]
        """
        # seconds = self._calculate_change_in_time(mesh, t)
        solution[inp[t].nonzero()] = inp[t][inp[t].nonzero()] 
        # vol = mesh[variables.VOLUME][t] + mesh[variables.GHOST_CELL_VOLUMES_IN][t] # + mesh[variables.GHOST_CELL_VOLUMES_OUT][t]
        self.vals[:] = self._calculate_rhs(self, mesh, t, solution)

    def _calculate_change_in_time(self, mesh, t):
        return mesh[variables.CHANGE_IN_TIME].values[t]
    
    def _calculate_volume(self, mesh, t):
        return mesh[variables.VOLUME][t][0:mesh.attrs.nreal]
    
    def _calculate_load(self, mesh, t, concentrations):
        volume = self._calculate_volume(mesh, t)
        delta_time = self._calculate_change_in_time(mesh, t)
        return volume * concentrations / delta_time
    
    def _calculate_ghost_cell_values(self, mesh, t):
        ghost_cells_in = np.zeros(mesh.attrs.nreal)
        ghost_cells_out = np.zeros(mesh.attrs.nreal)
        ghost_cells_in[:] = self._ghost_cell(mesh, t, flowing_in=True)[0:mesh.attrs.nreal]
        ghost_cells_out[:] = self._ghost_cell(mesh, t, flowing_in=False)[0:mesh.attrs.nreal]
        return ghost_cells_in, ghost_cells_out
    
    def _calculate_rhs(self, mesh, t, concentrations):
        load = self._calculate_load(mesh, t, concentrations)
        ghost_cells_in, ghost_cells_out = self._calculate_ghost_cell_values(self, mesh, t)
        return load + ghost_cells_in + ghost_cells_out


    def _transport_mechanisms(self, flowing_in):
        if flowing_in:
            advection = True
            diffusion = True
            condition = np.less
        else:
            advection = False
            diffusion = True
            condition = np.greater
        return advection, diffusion, condition
    
    def _define_arrays(self, mesh, advection, diffusion):
        advection_edge = None
        advection_face = None
        diffusion_edge = None
        diffusion_face = None

        if advection:
            advection_edge = np.zeros(len(mesh.nedge))
            advection_face = np.zeros(len(mesh.nface))
        if diffusion:
            diffusion_edge = np.zeros(len(mesh.nedge))
            diffusion_face = np.zeros(len(mesh.nface))
        return advection_edge, advection_face, diffusion_edge, diffusion_face
    
    def _edge_to_face(self, edge_array: np.array, face_array: np.array, mesh_array: xr.DataArray, index_list: list, internal_cell_index):
        edge_array[:] = abs(mesh_array[index_list])
        face_array[np.array(internal_cell_index)] = edge_array
        return face_array

    def _ghost_cell(self, mesh: xr.Dataset, t, flowing_in: bool, inp):
        advection, diffusion, condition = self._transport_mechanisms(flowing_in)
        advection_edge, advection_face, diffusion_edge, diffusion_face = self._define_arrays(advection, diffusion)

        velocity_indices = np.where(condition(mesh[variables.EDGE_VELOCITY][t], 0))[0]
        index_list = np.intersect(velocity_indices, self.ghost_cells)
        internal_cell_index = mesh[variables.EDGES_FACE1][velocity_indices]
        external_cell_index = mesh[variables.EDGES_FACE2][velocity_indices]

        concentratiton_multipliers = np.zeros(mesh.attrs.nface)
        concentratiton_multipliers[internal_cell_index] = inp[t][external_cell_index] 

        if len(index_list) != 0:
            if advection:
                advection_face[:] = self._edge_to_face(
                    advection_edge,
                    advection_face,
                    mesh[variables.ADVECTION_COEFFICIENT][t],
                    index_list,
                    internal_cell_index
                    )
            if diffusion:
                diffusion_face[:] = self._edge_to_face(
                    diffusion_edge,
                    diffusion_face,
                    mesh[variables.COEFFICIENT_TO_DIFFUSION_TERM][t],
                    index_list,
                    internal_cell_index
                    )
                
        if flowing_in:
            add_to_rhs = advection_face + diffusion_face
        else:
            add_to_rhs = diffusion_face
        
        return add_to_rhs * concentratiton_multipliers