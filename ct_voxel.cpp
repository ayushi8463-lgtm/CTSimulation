#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include <pybind11/stl.h>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace py = pybind11;
using Vec3 = std::array<double,3>;
struct Tri { Vec3 a,b,c; double denom,ymin,ymax; int64_t z0,z1; };

// Fill closed meshes by counting triangle crossings along x. Each OpenMP
// worker handles y rows; nested shells retain their cavities.
py::array_t<uint8_t> voxelize(
    py::array_t<double,py::array::c_style|py::array::forcecast> vertices,
    py::array_t<int64_t,py::array::c_style|py::array::forcecast> faces,
    std::array<double,3> origin, double pitch,
    std::array<int64_t,3> shape) {
    auto v=vertices.unchecked<2>(); auto f=faces.unchecked<2>();
    if(v.shape(1)!=3 || f.shape(1)!=3 || pitch<=0)
        throw std::invalid_argument("Expected Nx3 vertices/faces and positive pitch");
    const int64_t nx=shape[0],ny=shape[1],nz=shape[2];
    if(nx<1 || ny<1 || nz<1) throw std::invalid_argument("Voxel grid dimensions must be positive");
    py::array_t<uint8_t> result({nx,ny,nz}); auto out=result.mutable_unchecked<3>();
    const size_t total=(size_t)nx*ny*nz;
    std::fill(result.mutable_data(),result.mutable_data()+total,uint8_t(0));

    std::vector<Tri> tris; tris.reserve((size_t)f.shape(0));
    for(py::ssize_t ti=0;ti<f.shape(0);++ti) {
        const int64_t ia=f(ti,0),ib=f(ti,1),ic=f(ti,2);
        if(ia<0||ib<0||ic<0||ia>=v.shape(0)||ib>=v.shape(0)||ic>=v.shape(0)) continue;
        Tri t;
        t.a={v(ia,0),v(ia,1),v(ia,2)}; t.b={v(ib,0),v(ib,1),v(ib,2)}; t.c={v(ic,0),v(ic,1),v(ic,2)};
        const double v0y=t.b[1]-t.a[1],v0z=t.b[2]-t.a[2],v1y=t.c[1]-t.a[1],v1z=t.c[2]-t.a[2];
        t.denom=v0y*v1z-v1y*v0z;
        if(std::abs(t.denom)<1e-14*pitch*pitch) continue;
        t.ymin=std::min({t.a[1],t.b[1],t.c[1]}); t.ymax=std::max({t.a[1],t.b[1],t.c[1]});
        double zmin=std::min({t.a[2],t.b[2],t.c[2]}),zmax=std::max({t.a[2],t.b[2],t.c[2]});
        t.z0=std::max<int64_t>(0,(int64_t)std::ceil((zmin-origin[2])/pitch-0.5)-1);
        t.z1=std::min<int64_t>(nz-1,(int64_t)std::floor((zmax-origin[2])/pitch-0.5)+1);
        tris.push_back(t);
    }

    std::vector<std::vector<double>> crossings((size_t)ny*nz);
    const double bary_eps=1e-10;
    #pragma omp parallel for schedule(dynamic,1)
    for(int64_t y=0;y<ny;++y) {
        const double py=origin[1]+(y+0.5)*pitch;
        auto row_begin=crossings.begin()+(size_t)y*nz;
        for(const Tri &t:tris) {
            if(py<t.ymin-pitch || py>t.ymax+pitch) continue;
            const double v0y=t.b[1]-t.a[1],v0z=t.b[2]-t.a[2],v1y=t.c[1]-t.a[1],v1z=t.c[2]-t.a[2];
            for(int64_t z=t.z0;z<=t.z1;++z) {
                const double pz=origin[2]+(z+0.5)*pitch;
                const double v2y=py-t.a[1],v2z=pz-t.a[2];
                const double beta=(v2y*v1z-v1y*v2z)/t.denom;
                const double gamma=(v0y*v2z-v2y*v0z)/t.denom;
                const double alpha=1.0-beta-gamma;
                if(alpha>=-bary_eps&&beta>=-bary_eps&&gamma>=-bary_eps)
                    row_begin[z].push_back(alpha*t.a[0]+beta*t.b[0]+gamma*t.c[0]);
            }
        }
    }

    const double merge_eps=1e-6*pitch;
    #pragma omp parallel for collapse(2) schedule(static)
    for(int64_t y=0;y<ny;++y) for(int64_t z=0;z<nz;++z) {
        auto &hits=crossings[(size_t)y*nz+z]; std::sort(hits.begin(),hits.end());
        std::vector<double> unique_hits; unique_hits.reserve(hits.size());
        for(double x:hits) if(unique_hits.empty()||std::abs(x-unique_hits.back())>merge_eps) unique_hits.push_back(x);
        size_t k=0;
        for(int64_t x=0;x<nx;++x) {
            const double cx=origin[0]+(x+0.5)*pitch;
            while(k<unique_hits.size()&&unique_hits[k]<cx) ++k;
            out(x,y,z)=static_cast<uint8_t>(k&1U);
        }
    }
    return result;
}

PYBIND11_MODULE(ct_voxel,m) {
    m.doc()="Multithreaded C++ ray-parity STL voxelizer";
    m.def("voxelize",&voxelize,"Voxelize a triangle mesh using multithreaded ray parity");
}
