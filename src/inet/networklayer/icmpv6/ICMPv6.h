//
// Copyright (C) 2005 Andras Varga
// Copyright (C) 2005 Wei Yang, Ng
//
// This program is free software; you can redistribute it and/or
// modify it under the terms of the GNU Lesser General Public License
// as published by the Free Software Foundation; either version 2
// of the License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU Lesser General Public License for more details.
//
// You should have received a copy of the GNU Lesser General Public License
// along with this program; if not, see <http://www.gnu.org/licenses/>.
//

#ifndef __INET_ICMPV6_H
#define __INET_ICMPV6_H

#include "inet/common/INETDefs.h"
#include "inet/common/IProtocolRegistrationListener.h"
#include "inet/common/lifecycle/ILifecycle.h"
#include "inet/common/packet/Packet.h"
#include "inet/networklayer/icmpv6/ICMPv6Header_m.h"

namespace inet {

//foreign declarations:
class IPv6Address;
class Ipv6Header;
class PingPayload;

/**
 * ICMPv6 implementation.
 */
class INET_API ICMPv6 : public cSimpleModule, public ILifecycle, public IProtocolRegistrationListener
{
  public:
    /**
     *  This method can be called from other modules to send an ICMPv6 error packet.
     *  RFC 2463, Section 3: ICMPv6 Error Messages
     *  There are a total of 4 ICMPv6 error messages as described in the RFC.
     *  This method will construct and send error messages corresponding to the
     *  given type.
     *  Error Types:
     *      - Destination Unreachable Message - 1
     *      - Packet Too Big Message          - 2
     *      - Time Exceeded Message           - 3
     *      - Parameter Problem Message       - 4
     *  Code Types have different semantics for each error type. See RFC 2463.
     */
    virtual void sendErrorMessage(Packet *datagram, ICMPv6Type type, int code);

  protected:
    // internal helper functions
    virtual void sendToIP(Packet *msg, const IPv6Address& dest);
    virtual void sendToIP(Packet *msg);    // FIXME check if really needed

    virtual Packet *createDestUnreachableMsg(int code);
    virtual Packet *createPacketTooBigMsg(int mtu);
    virtual Packet *createTimeExceededMsg(int code);
    virtual Packet *createParamProblemMsg(int code);    //TODO:Section 3.4 describes a pointer. What is it?

  protected:
    /**
     * Initialization
     */
    virtual void initialize(int stage) override;
    virtual int numInitStages() const override { return NUM_INIT_STAGES; }

    /**
     *  Processing of messages that arrive in this module. Messages arrived here
     *  could be for ICMP ping requests or ICMPv6 messages that require processing.
     */
    virtual void handleMessage(cMessage *msg) override;
    virtual void processICMPv6Message(Packet *packet);

    virtual bool handleOperationStage(LifecycleOperation *operation, int stage, IDoneCallback *doneCallback) override;

    /**
     *  Respond to the machine that tried to ping us.
     */
    virtual void processEchoRequest(Packet *packet, const Ptr<const ICMPv6EchoRequestMsg>& header);

    /**
     *  Forward the ping reply to the "pingOut" of this module.
     */
    virtual void processEchoReply(Packet *packet, const Ptr<const ICMPv6EchoReplyMsg>& header);

    /**
     * Validate the received IPv6 datagram before responding with error message.
     */
    virtual bool validateDatagramPromptingError(Packet *packet);

    virtual void errorOut(const Ptr<const ICMPv6Header>& header);

    virtual void handleRegisterProtocol(const Protocol& protocol, cGate *gate) override;

  protected:
    typedef std::map<long, int> PingMap;
    PingMap pingMap;
    std::set<int> transportProtocols;    // where to send up packets
};

} // namespace inet

#endif // ifndef __INET_ICMPV6_H

