#include "scanner/api/kernel.h"
#include "scanner/api/op.h"
#include "scanner/util/memory.h"
#include "scanner/metadata.pb.h"

#include <boost/python.hpp>
#include <boost/python/numpy.hpp>

namespace scanner {

class PythonKernel : public BatchedKernel {
 public:
  PythonKernel(const KernelConfig& config, const std::string& kernel_str,
               const std::string& pickled_config,
               const int preferred_batch = 1);

  ~PythonKernel();

  void execute(const BatchedColumns& input_columns,
               BatchedColumns& output_columns) override;

 private:
  void batched_python_execute(const BatchedColumns& input_columns,
                              BatchedColumns& output_columns);
  void single_python_execute(const BatchedColumns& input_columns,
                             BatchedColumns& output_columns);
  KernelConfig config_;
  DeviceHandle device_;
  bool can_batch_;
};

}
