import six
import numpy as np

from .property_getter import ptype_ints

from yt.units.yt_array import YTQuantity, YTArray

UNBIND_HALOS    = True
UNBIND_GALAXIES = False
MINIMUM_STARS_PER_GALAXY = 32
MINIMUM_DM_PER_HALO      = 32

class GroupList(object):
    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner):
        if not hasattr(instance, '_%s' % self.name) or \
           isinstance(getattr(instance, '_%s' % self.name), int):
            from .loader import restore_single_list
            restore_single_list(instance.obj, instance, self.name)
        return getattr(instance, '_%s' % self.name)

    def __set__(self, instance, value):
        setattr(instance, '_%s' % self.name, value)


class Group(object):
    glist = GroupList('glist')
    slist = GroupList('slist')    
    
    def __init__(self,obj):
        self.particle_indexes = []
        self.obj = obj

        self.masses = {}
        self.radii = {} 
        self.temperatures = {}

    @property
    def valid(self):
        if self.obj_type == 'halo' and self.ndm < MINIMUM_DM_PER_HALO:
            return False
        elif self.obj_type == 'galaxy' and self.nstar < MINIMUM_STARS_PER_GALAXY:
            return False
        else:
            return True

    def _delete_attribute(self,a):
        if hasattr(self,a):
            delattr(self,a)
    
    def _cleanup(self):
        self._delete_attribute('particle_data')
        self._delete_attribute('particle_indexes')
        self._delete_attribute('_pdata')
    
    def _process_group(self,pdata):
        self._pdata = pdata
        self._assign_particle_data()
        self._assign_local_indexes()

        if self.valid:
            self._calculate_total_mass()
            self._calculate_center_of_mass_quantities()
            self._unbind()  # iterative procedure

            if self.valid:
                self._calculate_masses()
                self._calculate_virial_quantities()
                self._calculate_velocity_dispersions()
                self._calculate_angular_quantities()            
                self._assign_global_plists()
            
        self._cleanup()
        
    def _assign_particle_data(self):
        """ Use self.particle_indexes to assign group particle data """
        self.particle_data = {}
        for k,v in six.iteritems(self._pdata):
            self.particle_data[k] = v[self.particle_indexes]

    def _assign_local_indexes(self):
        """ glist/slist/dmlist indexes correspond to the LOCAL particle data """
        self.glist  = np.where(self.particle_data['ptype'] == ptype_ints['gas'])[0] 
        self.slist  = np.where(self.particle_data['ptype'] == ptype_ints['star'])[0]
        self.dmlist = np.where(self.particle_data['ptype'] == ptype_ints['dm'])[0]

        self.ngas  = len(self.glist)
        self.nstar = len(self.slist)
        self.ndm   = len(self.dmlist)
        
    def _assign_global_plists(self):
        """ glist/slist/dmlist indexes correspond to the GLOBAL particle data """
        if isinstance(self.particle_indexes, list):
            self.particle_indexes = np.array(self.particle_indexes)
        self.glist  = self.particle_data['indexes'][self.glist]
        self.slist  = self.particle_data['indexes'][self.slist]
        self.dmlist = self.particle_data['indexes'][self.dmlist]

    def _calculate_total_mass(self):
        self.masses['total'] = self.obj.yt_dataset.quan(np.sum(self.particle_data['mass']), self.obj.units['mass'])
        
    def _calculate_masses(self):
        """ calculate various masses """
        mass_dm     = np.sum(self.particle_data['mass'][self.dmlist])
        mass_gas    = np.sum(self.particle_data['mass'][self.glist])
        mass_star   = np.sum(self.particle_data['mass'][self.slist])
        mass_baryon = mass_gas + mass_star

        self.masses['dm']      = self.obj.yt_dataset.quan(mass_dm, self.obj.units['mass'])
        self.masses['gas']     = self.obj.yt_dataset.quan(mass_gas, self.obj.units['mass'])
        self.masses['stellar'] = self.obj.yt_dataset.quan(mass_star, self.obj.units['mass'])
        self.masses['baryon']  = self.obj.yt_dataset.quan(mass_baryon, self.obj.units['mass'])

        self._calculate_total_mass()
            
    def _calculate_center_of_mass_quantities(self):
        """ calculate center-of-mass position and velocity """
        def get_center_of_mass_quantity(quantity):  ## REFACTOR ME TO BE MORE GENERIC WITH SHAPE
            val  = np.zeros(3)
            for i in range(0,3):
                val[i] = np.sum(self.particle_data['mass'] * self.particle_data[quantity][:,i])
            val /= self.masses['total'].d
            return val

        self.pos = self.obj.yt_dataset.arr(get_center_of_mass_quantity('pos'), self.obj.units['length'])
        self.vel = self.obj.yt_dataset.arr(get_center_of_mass_quantity('vel'), self.obj.units['velocity'])

    def _unbind(self):
        if self.obj_type == 'halo' and not UNBIND_HALOS:
            return
        elif self.obj_type == 'galaxy' and not UNBIND_GALAXIES:
            return        

        if not hasattr(self, 'unbound_indexes'):
            self.unbound_indexes = {
                ptype_ints['gas']:[],
                ptype_ints['star']:[],
                ptype_ints['dm']:[],
            }
        if not hasattr(self, 'unbind_iterations'):
            self.unbind_iterations = 0        
        self.unbind_iterations += 1
        
        cmpos = (self.pos.to('kpc')).d
        ppos  = self.obj.yt_dataset.arr(self.particle_data['pos'], self.obj.units['length'])
        ppos  = (ppos.to('kpc')).d
        cmvel = (self.vel.to('kpc/s')).d
        pvels = self.obj.yt_dataset.arr(self.particle_data['vel'], self.obj.units['velocity'])
        pvels = (pvels.to('kpc/s')).d
        mass  = self.obj.yt_dataset.arr(self.particle_data['mass'], self.obj.units['mass'])
        mass  = (mass.to('Msun')).d
        
        r  = np.sqrt( (ppos[:,0] - cmpos[0])**2 +
                      (ppos[:,1] - cmpos[1])**2 +
                      (ppos[:,2] - cmpos[2])**2 )
        v2 = ( (pvels[:,0] - cmvel[0])**2 +
               (pvels[:,1] - cmvel[1])**2 +
               (pvels[:,2] - cmvel[2])**2 )
        
        energy = -(mass * self.obj.simulation.G.d * (self.masses['total'].d - mass) / r) + (0.5 * mass * v2)

        positive = np.where(energy > 0)[0]
        if len(positive) > 0:
            positive = positive[::-1]
            for i in positive:
                self.unbound_indexes[self.particle_data['ptype'][i]].append(self.particle_data['indexes'][i])
                del self.particle_indexes[i]

            self._assign_particle_data()
            self._assign_local_indexes()

            if not self.valid:
                return
            
            self._calculate_total_mass()
            self._calculate_center_of_mass_quantities()
            self._unbind()
        
    def _calculate_virial_quantities(self):
        # from Byran & Norman 1998 (xray cluster paper)
        # and Mo et al. 2002
        rho_crit = self.obj.simulation.critical_density   # in Msun/kpc^3 PHYSICAL

        def get_r_vir(deltaC):
            """ returns r_vir in PHYSICAL kpc """
            return ( (3.0 * self.masses['total'].to('Msun') /
                      (4.0 * np.pi * rho_crit * deltaC))**(1./3.) )

        # Bryan & Norman 1998
        self.radii['virial'] = YTQuantity(get_r_vir(18.0 * np.pi**2), 'kpc', registry=self.obj.yt_dataset.unit_registry)
        self.radii['r200c']  = YTQuantity(get_r_vir(200.0), 'kpc', registry=self.obj.yt_dataset.unit_registry)

        # equation 1 of Mo et al 2002
        vc = (np.sqrt( self.obj.simulation.G * self.masses['total'].to('Msun') / self.radii['r200c'] )).to('km/s')

        # equation 4 of Mo et al 2002 (K)
        vT = YTQuantity(3.6e5 * (vc.d / 100.0)**2, 'K', registry=self.obj.yt_dataset.unit_registry)

        self.radii['virial'] = self.radii['virial'].to(self.obj.units['length'])
        self.radii['r200c']  = self.radii['r200c'].to(self.obj.units['length'])

        vc = vc.to(self.obj.units['velocity'])
        vT = vT.to(self.obj.units['temperature'])

        self.temperatures['virial'] = vT
        
        self.virial_quantities = dict(
            radius = self.radii['virial'],
            r200c  = self.radii['r200c'],
            circular_velocity = vc,
            temperature = vT
        )

    def _calculate_velocity_dispersions(self):
        def get_sigma(filtered_v):
            if len(filtered_v) == 0:
                return 0.0            
            v_mean = np.mean(filtered_v)
            v_diff = filtered_v - v_mean
            return np.std(v_diff)

        ptypes = self.particle_data['ptype']
        particle_vel = self.particle_data['vel']
        v = np.sqrt( particle_vel[:,0]**2 +
                     particle_vel[:,1]**2 +
                     particle_vel[:,2]**2 )
            
        self.velocity_dispersions = dict() 

        self.velocity_dispersions['all']     = get_sigma(v)
        self.velocity_dispersions['dm']      = get_sigma(v[ptypes == ptype_ints['dm']])
        self.velocity_dispersions['baryon']  = get_sigma(v[(ptypes == ptype_ints['gas']) | (ptypes == ptype_ints['star'])])
        self.velocity_dispersions['gas']     = get_sigma(v[ptypes == ptype_ints['gas']])
        self.velocity_dispersions['stellar'] = get_sigma(v[ptypes == ptype_ints['star']])

        for k,v in six.iteritems(self.velocity_dispersions):
            self.velocity_dispersions[k] = YTQuantity(v, self.obj.units['velocity'], registry=self.obj.yt_dataset.unit_registry)

            
    def _calculate_angular_quantities(self):
        px = self.obj.yt_dataset.arr(self.particle_data['mass'] * self.particle_data['vel'][:,0], '%s * %s' % (self.obj.units['mass'],self.obj.units['velocity']))
        py = self.obj.yt_dataset.arr(self.particle_data['mass'] * self.particle_data['vel'][:,1], '%s * %s' % (self.obj.units['mass'],self.obj.units['velocity']))
        pz = self.obj.yt_dataset.arr(self.particle_data['mass'] * self.particle_data['vel'][:,2], '%s * %s' % (self.obj.units['mass'],self.obj.units['velocity']))

        x  = (self.obj.yt_dataset.arr(self.particle_data['pos'][:,0] - self.pos[0].d, self.obj.units['length'])).to('km')
        y  = (self.obj.yt_dataset.arr(self.particle_data['pos'][:,1] - self.pos[1].d, self.obj.units['length'])).to('km')
        z  = (self.obj.yt_dataset.arr(self.particle_data['pos'][:,2] - self.pos[2].d, self.obj.units['length'])).to('km')
        
        Lx = np.sum( y*pz - z*py )
        Ly = np.sum( z*px - x*pz )
        Lz = np.sum( x*py - y*px )
        self.total_angular_momentum  = np.sqrt(Lx*Lx + Ly*Ly + Lz*Lz)
        self.angular_momentum_vector = self.obj.yt_dataset.arr([Lx.d,Ly.d,Lz.d], Lx.units)

        # Bullock spin or lambda prime
        self.spin = self.total_angular_momentum / (1.4142135623730951 *
                                                   self.masses['total'] *
                                                   self.virial_quantities['circular_velocity'].to('km/s') *
                                                   self.virial_quantities['r200c'].to('km'))        
        
class Galaxy(Group):
    obj_type = 'galaxy'    
    def __init__(self,obj):
        super(Galaxy, self).__init__(obj)
        self.central = False
        
class Halo(Group):
    obj_type = 'halo'
    dmlist   = GroupList('dmlist')
    def __init__(self,obj):
        super(Halo, self).__init__(obj)
        self.child = False

def create_new_group(obj, group_type):
    if group_type == 'halo':
        return Halo(obj)
    elif group_type == 'galaxy':
        return Galaxy(obj)
