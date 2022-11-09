#ifndef BAMNVME_H
#define BAMNVME_H

#define TYPE float

struct BAM_Feature_Store {
  const char *const ctrls_paths[1] = {"/dev/libnvm0"};

  uint32_t cudaDevice = 0;
  uint64_t cudaDeviceId = 0;
//  const char *blockDevicePath = nullptr;
//  const char *controllerPath = nullptr;
  uint64_t controllerId = 0;
  uint32_t adapter = 0;
  uint32_t segmentId = 0;
  uint32_t nvmNamespace = 1;
  bool doubleBuffered = false;
  size_t numReqs = 100;
  size_t numPages = 262144 * 4 / 2;
  // size_t numPages = 1024*100 ;
  
  size_t startBlock = 0;


  bool stats = false;
//  const char *output = nullptr;
  size_t numThreads = 64;
  uint32_t domain = 0;
  uint32_t bus = 0;
  uint32_t devfn = 0;

  uint32_t n_ctrls = 1;
  size_t blkSize = 64;
  size_t queueDepth = 1024;
  size_t numQueues = 128;
  size_t pageSize = 1024 ;
  uint64_t numElems = 979611600;
  // std::string name;

  std::vector<Controller *> ctrls;
  page_cache_t *h_pc;
  range_t<TYPE> *h_range;

//  page_cache_t *d_pc;
 // range_t<TYPE> *d_range;

  std::vector<range_t<TYPE> *> vr;
  //std::vector<array_t<TYPE>> a_vec;
  array_t<TYPE> *a;
  BAM_Feature_Store()  {};

  ~BAM_Feature_Store(){
 
	  for(auto i : ctrls){
	  	delete(i);
	  }
	  delete(h_pc);
	  delete(h_range);
	  delete(a);
  }
  // BAM_Feature_Store(const std::string &name)
  //   : name(name) {}
  // void init_controllers(const char* const ctrls_paths[], uint32_t
  // nvmNamespace, uint32_t cudaDevice, uint64_t queueDepth, uint64_t numQueues,
  // int num_controllers, std::vector<Controller*> &ctrls_vec);

  void print();
  int add(int a, int b);
  void init_controllers();
  void read_feature_test();
  void read_feature(uint64_t tensor_ptr, uint64_t index_ptr,int64_t num_index, int dim);

};

#endif
